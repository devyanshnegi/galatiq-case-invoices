"""Streamlit dashboard for the AP agents.   Run:  streamlit run app.py"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from invoice_agents.agents.payment import resolve_pending
from invoice_agents.config import PROJECT_ROOT, load_settings
from invoice_agents.db import Store
from invoice_agents.graph import NODE_LABELS, InvoicePipeline
from invoice_agents.llm import DEFAULT_MODELS

SAMPLES_DIR = PROJECT_ROOT / "data" / "invoices"
UPLOAD_DIR = PROJECT_ROOT / "uploads"
SUFFIXES = {".txt", ".pdf", ".json", ".csv", ".xml"}
STATUS_BADGE = {"paid": "🟢 Paid", "rejected": "🔴 Rejected", "pending_review": "🟡 Needs human review", "failed": "🟣 Failed"}
SEVERITY_ICON = {"error": "⛔", "warning": "⚠️", "info": "ℹ️"}

st.set_page_config(page_title="Acme AP Agents", page_icon="🧾", layout="wide")


@st.cache_resource(show_spinner="Connecting to model…")
def get_pipeline(provider: str, model: str | None) -> InvoicePipeline:
    return InvoicePipeline(load_settings(provider=provider, model=model or None))


def md(text) -> str:
    """Escape '$' so Streamlit markdown doesn't render dollar amounts as LaTeX."""
    return str(text).replace("$", r"\$")


def money(value, currency: str = "USD") -> str:
    if value is None:
        return "—"
    return f"${value:,.2f}" if currency == "USD" else f"{value:,.2f} {currency}"


# ---- sidebar ------------------------------------------------------------------------------------

env_settings = load_settings()
with st.sidebar:
    st.header("🧾 Acme AP Agents")
    providers = list(DEFAULT_MODELS)
    provider = st.selectbox("LLM provider", providers, index=providers.index(env_settings.llm.provider) if env_settings.llm.provider in providers else 0,
                            help="Swap the reasoning engine without code changes. 'offline' runs rule-based agents with no model.")
    model = st.text_input("Model", value=env_settings.llm.model or DEFAULT_MODELS[provider] if provider == env_settings.llm.provider else DEFAULT_MODELS[provider])
    pipeline = get_pipeline(provider, model)
    store = pipeline.store
    if pipeline.llm.is_offline and provider != "offline":
        st.warning(f"**{provider}** isn't reachable or has no API key — running in offline rule-based mode.")
    else:
        st.success(f"Using **{pipeline.llm.label}**")

    st.divider()
    policy = pipeline.settings.policy
    st.caption("APPROVAL POLICY")
    st.markdown(md(f"- VP scrutiny ≥ **${policy.vp_review_threshold:,.0f}**\n- Human sign-off ≥ **${policy.cfo_threshold:,.0f}**\n"
                   f"- Price tolerance **{policy.price_variance_tolerance:.0%}** over catalog"))
    st.divider()
    if st.button("🗑️ Reset ledger", help="Clear processed invoices, payments and audit log (inventory is kept)."):
        store.reset_ledger()
        st.session_state.pop("last_state", None)
        st.session_state.pop("batch", None)
        st.toast("Ledger cleared")


# ---- result rendering ---------------------------------------------------------------------------


def render_state(state: dict) -> None:
    inv = state.get("invoice") or {}
    status = state.get("status", "failed")
    if status == "failed":
        st.error(f"Could not process this document: {state.get('error')}")
        return
    decision = state.get("decision") or {}
    validation = state.get("validation") or {}
    pol = state.get("policy") or {}

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Outcome", STATUS_BADGE.get(status, status))
    c2.metric("Amount", money(inv.get("total"), inv.get("currency", "USD")),
              help=f"≈ {money(validation.get('total_usd'))} USD" if inv.get("currency") != "USD" else None)
    c3.metric("Risk score", f"{validation.get('risk_score', 0)}/100")
    c4.metric("Approval tier", pol.get("tier", "—").upper())

    box = {"paid": st.success, "rejected": st.error, "pending_review": st.warning}.get(status, st.info)
    box(f"**{decision.get('decision', '?').upper()}** — {md(decision.get('reasoning', ''))}")
    if decision.get("conditions"):
        st.markdown("**Conditions:** " + md(" · ".join(decision["conditions"])))

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Extracted invoice")
        st.caption(f"{state.get('source_format', '?').upper()} · extracted via **{state.get('extraction_method')}**"
                   f" · {state.get('extraction_attempts') or 1} attempt(s)")
        st.markdown(
            f"**{inv.get('invoice_number') or '?'}** from **{inv.get('vendor') or '❓ missing vendor'}**  \n"
            f"Issued {inv.get('invoice_date') or '—'} · Due **{inv.get('due_date') or (inv.get('due_date_raw') or 'missing')}** · {inv.get('payment_terms') or 'no terms'}"
        )
        rows = [{"Item": li["item"], "Matched SKU": li.get("sku") or "❌ not in catalog", "Qty": li.get("quantity"),
                 "Unit price": li.get("unit_price"), "Amount": li.get("amount"), "Note": li.get("note") or ""} for li in inv.get("line_items", [])]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        totals = {k: inv.get(k) for k in ("subtotal", "tax", "shipping", "total") if inv.get(k) is not None}
        st.caption(md(" · ".join(f"{k.title()}: {money(v, inv.get('currency', 'USD'))}" for k, v in totals.items())))
    with right:
        st.subheader("Validation findings")
        issues = validation.get("issues", [])
        if not issues:
            st.success("All checks passed.")
        for i in issues:
            st.markdown(f"{SEVERITY_ICON[i['severity']]} **{i['code']}** — {md(i['message'])}")

    critiques = state.get("critiques") or []
    with st.expander(f"🔁 Reflection loop — {len(critiques)} critique round(s)", expanded=len(critiques) > 1):
        for c in critiques:
            verdict = "✅ Internal audit agreed" if c["agrees"] else f"↩️ Internal audit pushed back → recommends **{c['recommended_decision']}**"
            st.markdown(f"**Round {c['round']}** ({c['method']}): {verdict}")
            for p in c.get("problems", []):
                st.markdown(f"  - {md(p)}")
        if decision.get("override"):
            st.warning(f"Policy guardrail override: {md(decision['override'])}")

    with st.expander("🧭 Agent trace"):
        trace = [{"stage": e["stage"], "event": e["event"], "ms": e.get("ms"),
                  "detail": json.dumps(e["detail"], default=str)[:300]} for e in state.get("trace", [])]
        st.dataframe(pd.DataFrame(trace), hide_index=True, use_container_width=True)
    if state.get("risk") and not state["risk"].get("skipped"):
        with st.expander("🕵️ AI risk analyst"):
            st.json(state["risk"])
    with st.expander("📄 Source document"):
        st.code(state.get("raw_text", ""), language=None)


def run_with_progress(path: Path) -> dict:
    state: dict = {}
    with st.status(f"Processing {path.name}…", expanded=True) as status, contextlib.redirect_stdout(io.StringIO()):
        for node, update, state in pipeline.stream(path):
            events = update.get("trace") or []
            ms = sum(e.get("ms") or 0 for e in events)
            st.write(f"✓ {NODE_LABELS.get(node, node)} · {ms} ms")
        status.update(label=f"{path.name}: {STATUS_BADGE.get(state.get('status'), state.get('status'))}", state="complete", expanded=False)
    return state


# ---- tabs ---------------------------------------------------------------------------------------

pending_count = len(store.list_invoices("pending_review"))
tab_process, tab_queue, tab_ledger, tab_about = st.tabs(
    ["▶️ Process invoices", f"🙋 Review queue ({pending_count})", "📒 Ledger & audit", "🧠 How it works"]
)

with tab_process:
    samples = sorted(p for p in SAMPLES_DIR.iterdir() if p.suffix.lower() in SUFFIXES)
    col_a, col_b = st.columns([2, 1])
    with col_a:
        choice = st.selectbox("Sample invoice", samples, format_func=lambda p: p.name)
        upload = st.file_uploader("…or upload your own", type=[s.lstrip(".") for s in SUFFIXES])
    with col_b:
        st.write("")
        run_one = st.button("Process invoice", type="primary", use_container_width=True)
        run_all = st.button(f"Process all {len(samples)} samples", use_container_width=True)
        fresh = st.checkbox("Start batch from a clean ledger", value=True,
                            help="Otherwise invoices already processed are (correctly) rejected as duplicates.")

    if run_one:
        path = choice
        if upload is not None:
            UPLOAD_DIR.mkdir(exist_ok=True)
            path = UPLOAD_DIR / Path(upload.name).name
            path.write_bytes(upload.getvalue())
        st.session_state["last_state"] = run_with_progress(path)
        st.session_state.pop("batch", None)

    if run_all:
        if fresh:
            store.reset_ledger()
        progress = st.progress(0.0, text="Starting batch…")
        batch = []
        for n, path in enumerate(samples, start=1):
            with contextlib.redirect_stdout(io.StringIO()):
                state = pipeline.process(path)
            batch.append((path.name, state))
            progress.progress(n / len(samples), text=f"{path.name}: {STATUS_BADGE.get(state.get('status'))}")
        st.session_state["batch"] = batch
        st.session_state.pop("last_state", None)

    if st.session_state.get("batch"):
        batch = st.session_state["batch"]
        totals = {}
        for _, s in batch:
            usd = (s.get("validation") or {}).get("total_usd") or 0
            totals[s.get("status")] = totals.get(s.get("status"), 0) + max(usd, 0)
        m1, m2, m3, m4 = st.columns(4)
        for col, label, key, suffix in ((m1, "Paid automatically", "paid", "paid"), (m2, "Rejected", "rejected", "blocked"),
                                        (m3, "Needs a human", "pending_review", "on hold"), (m4, "Failed", "failed", "")):
            col.metric(label, sum(1 for _, s in batch if s.get("status") == key))
            if suffix:
                col.caption(md(f"{money(totals.get(key, 0))} {suffix}"))
        table = []
        for name, s in batch:
            inv = s.get("invoice") or {}
            issues = [i for i in (s.get("validation") or {}).get("issues", []) if i["severity"] != "info"]
            table.append({"File": name, "Invoice": inv.get("invoice_number"), "Vendor": inv.get("vendor"),
                          "Amount": money(inv.get("total"), inv.get("currency", "USD")), "Outcome": STATUS_BADGE.get(s.get("status")),
                          "Why": issues[0]["message"] if issues else "All checks passed"})
        st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)
        pick = st.selectbox("Inspect one", [n for n, _ in batch])
        render_state(dict(batch)[pick])

    elif st.session_state.get("last_state"):
        render_state(st.session_state["last_state"])

with tab_queue:
    pending = store.list_invoices("pending_review")
    if not pending:
        st.success("Nothing waiting — every processed invoice was resolved automatically.")
    for row in pending:
        result = json.loads(row["result_json"])
        with st.container(border=True):
            st.markdown(f"### {row['invoice_number']} · {row['vendor']} · {md(money(row['total'], row['currency']))}")
            st.markdown(f"**Why a human is needed:** {md(row['reasoning'])}")
            for i in result.get("validation", {}).get("issues", []):
                if i["severity"] != "info":
                    st.markdown(f"{SEVERITY_ICON[i['severity']]} **{i['code']}** — {md(i['message'])}")
            note = st.text_input("Note", key=f"note-{row['id']}", placeholder="e.g. PO amendment confirmed with purchasing")
            reviewer = st.text_input("Your name", key=f"who-{row['id']}", value="VP Finance")
            b1, b2, _ = st.columns([1, 1, 4])
            if b1.button("✅ Approve & pay", key=f"ok-{row['id']}", type="primary"):
                with contextlib.redirect_stdout(io.StringIO()):
                    resolve_pending(store, row["id"], approve=True, reviewer=reviewer, note=note)
                st.toast(f"{row['invoice_number']} approved and paid")
                st.rerun()
            if b2.button("⛔ Reject", key=f"no-{row['id']}"):
                resolve_pending(store, row["id"], approve=False, reviewer=reviewer, note=note)
                st.toast(f"{row['invoice_number']} rejected")
                st.rerun()

with tab_ledger:
    invoices = store.list_invoices()
    payments = store.list_payments()
    if invoices:
        processed = len(invoices)
        straight_through = sum(1 for r in invoices if r["status"] in ("paid", "rejected")
                               and not (r["reasoning"] or "").startswith(("Approved by", "Rejected by")))
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Invoices processed", processed)
        k2.metric("Straight-through rate", f"{straight_through / processed:.0%}", help="Resolved with no human touch")
        k3.metric("Paid", money(sum(p["amount"] or 0 for p in payments)))
        k4.metric("Blocked (rejected)", money(sum(r["total"] or 0 for r in invoices if r["status"] == "rejected" and (r["total"] or 0) > 0)))
    st.subheader("Invoices")
    st.dataframe(pd.DataFrame(invoices, columns=["id", "invoice_number", "vendor", "total", "currency", "status", "decision", "reasoning", "source_file", "processed_at"])
                 if invoices else pd.DataFrame(), hide_index=True, use_container_width=True)
    st.subheader("Payments (mock bank API)")
    st.dataframe(pd.DataFrame(payments), hide_index=True, use_container_width=True)
    st.subheader("Inventory")
    st.dataframe(pd.DataFrame(store.list_inventory()), hide_index=True, use_container_width=True)
    with st.expander("Audit log (latest 500 events)"):
        st.dataframe(pd.DataFrame(store.list_audit()), hide_index=True, use_container_width=True)

with tab_about:
    st.markdown("""
#### Four agents, one graph
1. **Ingestion** — JSON/CSV/XML are parsed deterministically; free text and PDFs go to the LLM with a JSON schema.
   A **self-check** verifies every extracted value actually appears in the source and loops back with the specific problems (max 3 attempts).
2. **Validation** — code checks stock, catalog, arithmetic, dates, duplicates, vendor master and currency. An **LLM risk analyst** adds the judgement calls rules miss.
3. **Approval** — a **VP reviewer** agent investigates with **tool calls** (policy, inventory, vendor master, history) and drafts a decision;
   an **internal-audit critic** challenges it and the reviewer revises (reflection loop). A **code guardrail** enforces the policy no matter what the LLMs say.
4. **Payment** — approved invoices hit the mock bank API (idempotently); rejections are logged with reasoning; escalations wait in the **review queue**.
""")
    st.graphviz_chart("""
digraph G { rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#eef2ff", fontname="Helvetica"];
  load -> extract [label="free text"]; load -> self_check [label="structured"]; load -> failed [label="unreadable"];
  extract -> self_check; self_check -> extract [label="problems found", color="#d97706", fontcolor="#d97706"];
  self_check -> validate; validate -> review; review -> critique;
  critique -> review [label="audit disagrees", color="#d97706", fontcolor="#d97706"];
  critique -> guardrail [label="agrees / max rounds"]; guardrail -> payment;
  payment [fillcolor="#dcfce7"]; failed [fillcolor="#fee2e2"]; }
""")
