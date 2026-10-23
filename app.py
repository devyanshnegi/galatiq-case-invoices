"""Streamlit dashboard for the AP agents.   Run:  streamlit run app.py"""

from __future__ import annotations

import contextlib
import io
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import streamlit as st

from invoice_agents.agents.payment import resolve_pending
from invoice_agents.config import PROJECT_ROOT, load_settings
from invoice_agents.db import Store
from invoice_agents.graph import NODE_LABELS, InvoicePipeline
from invoice_agents.jobqueue import JobQueue
from invoice_agents.llm import DEFAULT_MODELS

SAMPLES_DIR = PROJECT_ROOT / "data" / "invoices"
UPLOAD_DIR = PROJECT_ROOT / "uploads"
SUFFIXES = {".txt", ".pdf", ".json", ".csv", ".xml"}
STATUS_BADGE = {"paid": "🟢 Paid", "rejected": "🔴 Rejected", "pending_review": "🟡 Needs human review", "failed": "🟣 Failed"}
SEVERITY_ICON = {"error": "⛔", "warning": "⚠️", "info": "ℹ️"}

st.set_page_config(page_title="Acme AP Agents", page_icon="🧾", layout="wide")


@st.cache_resource(show_spinner="Connecting to model and tools…")
def get_pipeline(provider: str, model: str | None, backend: str, fast_path: bool) -> InvoicePipeline:
    return InvoicePipeline(load_settings(provider=provider, model=model or None, tools_backend=backend, fast_path=fast_path))


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
    backend = st.radio("ERP & bank tools", ["local", "mcp"], horizontal=True,
                       index=["local", "mcp"].index(env_settings.tools_backend),
                       help="'mcp' runs the ERP and bank as separate MCP servers the agents call over the protocol.")
    fast_path = st.toggle("Fast path", value=env_settings.fast_path,
                          help="Skip LLM deliberation when the policy allows only one outcome (clean -> pay, blocked -> reject).")
    workers = st.slider("Batch workers", 1, 16, env_settings.workers, help="Invoices processed concurrently in a batch.")
    pipeline = get_pipeline(provider, model, backend, fast_path)
    store = pipeline.store
    if pipeline.llm.is_offline and provider != "offline":
        st.warning(f"**{provider}** isn't reachable or has no API key — running in offline rule-based mode.")
    else:
        st.success(f"Using **{pipeline.llm.label}** · tools via **{pipeline.backend.name}**")

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
        confidence = state.get("extraction_confidence")
        st.caption(f"{state.get('source_format', '?').upper()} · extracted via **{state.get('extraction_method')}**"
                   f" · {state.get('extraction_attempts') or 1} attempt(s)"
                   + (f" · confidence **{confidence:.0%}**" if confidence is not None else "")
                   + f" · {state.get('llm_calls', 0)} LLM call(s)")
        st.markdown(
            f"**{inv.get('invoice_number') or '?'}** from **{inv.get('vendor') or '❓ missing vendor'}**  \n"
            f"Issued {inv.get('invoice_date') or '—'} · Due **{inv.get('due_date') or (inv.get('due_date_raw') or 'missing')}** · {inv.get('payment_terms') or 'no terms'}"
        )
        rows = [{"Item": li["item"], "Matched SKU": li.get("sku") or "❌ not in catalog", "Qty": li.get("quantity"),
                 "Unit price": li.get("unit_price"), "Amount": li.get("amount"), "Note": li.get("note") or ""} for li in inv.get("line_items", [])]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
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
        st.dataframe(pd.DataFrame(trace), hide_index=True, width="stretch")
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
tab_process, tab_queue, tab_ops, tab_ledger, tab_about = st.tabs(
    ["▶️ Process invoices", f"🙋 Review queue ({pending_count})", "📈 Operations", "📒 Ledger & audit", "🧠 How it works"]
)

with tab_process:
    samples = sorted(p for p in SAMPLES_DIR.iterdir() if p.suffix.lower() in SUFFIXES)
    col_a, col_b = st.columns([2, 1])
    with col_a:
        choice = st.selectbox("Sample invoice", samples, format_func=lambda p: p.name)
        upload = st.file_uploader("…or upload your own", type=[s.lstrip(".") for s in SUFFIXES])
    with col_b:
        st.write("")
        run_one = st.button("Process invoice", type="primary", width="stretch")
        run_all = st.button(f"Process all {len(samples)} samples", width="stretch")
        fresh = st.checkbox("Start from a clean ledger", value=True,
                            help="Applies to both buttons. Clears processed invoices, payments, the review queue and "
                                 "learned precedents first. Untick to keep history - then re-processing an invoice "
                                 "is (correctly) rejected as a duplicate.")

    if (run_one or run_all) and fresh:
        store.reset_ledger()
        st.session_state.pop("last_state", None)
        st.session_state.pop("batch", None)

    if run_one:
        path = choice
        if upload is not None:
            UPLOAD_DIR.mkdir(exist_ok=True)
            path = UPLOAD_DIR / Path(upload.name).name
            path.write_bytes(upload.getvalue())
        st.session_state["last_state"] = run_with_progress(path)
        st.session_state.pop("batch", None)
        st.rerun()  # refresh tab badges (review queue count) and other tabs from the new ledger

    if run_all:
        progress = st.progress(0.0, text=f"Starting batch with {workers} workers…")
        results: dict[str, dict] = {}
        with contextlib.redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(pipeline.process, path): path for path in samples}
            for n, future in enumerate(as_completed(futures), start=1):  # UI updates stay on Streamlit's thread
                path = futures[future]
                results[path.name] = future.result()
                progress.progress(n / len(samples), text=f"{path.name}: {STATUS_BADGE.get(results[path.name].get('status'))}")
        batch = [(p.name, results[p.name]) for p in samples]
        st.session_state["batch"] = batch
        st.session_state.pop("last_state", None)
        st.rerun()

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
        st.dataframe(pd.DataFrame(table), hide_index=True, width="stretch")
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
            precedents = store.vendor_precedents(row["vendor"], limit=3)
            if precedents:
                st.info("📚 **Earlier human decisions for this vendor:** " + md(" · ".join(
                    f"{p['decision']} {p['invoice_number']} by {p['reviewer']} ({p['decided_at'][:10]})"
                    + (f" - \"{p['note']}\"" if p.get("note") else "") for p in precedents)))
            for i in result.get("validation", {}).get("issues", []):
                if i["severity"] != "info":
                    st.markdown(f"{SEVERITY_ICON[i['severity']]} **{i['code']}** — {md(i['message'])}")
            note = st.text_input("Note", key=f"note-{row['id']}", placeholder="e.g. PO amendment confirmed with purchasing")
            reviewer = st.text_input("Your name", key=f"who-{row['id']}", value="VP Finance")
            b1, b2, _ = st.columns([1, 1, 4])
            if b1.button("✅ Approve & pay", key=f"ok-{row['id']}", type="primary"):
                with contextlib.redirect_stdout(io.StringIO()):
                    resolve_pending(store, row["id"], approve=True, reviewer=reviewer, note=note, backend=pipeline.backend)
                st.toast(f"{row['invoice_number']} approved and paid")
                st.rerun()
            if b2.button("⛔ Reject", key=f"no-{row['id']}"):
                resolve_pending(store, row["id"], approve=False, reviewer=reviewer, note=note, backend=pipeline.backend)
                st.toast(f"{row['invoice_number']} rejected")
                st.rerun()

with tab_ops:
    rows = store.list_invoices()
    if not rows:
        st.info("Process some invoices to see operational metrics.")
    else:
        df = pd.DataFrame(rows)
        n = len(df)
        human = df["reasoning"].fillna("").str.startswith(("Approved by", "Rejected by"))
        straight_through = ((df["status"].isin(["paid", "rejected"])) & ~human).sum() / n
        durations = df["duration_ms"].dropna()
        audit = pd.DataFrame(store.list_audit(limit=20000))
        fast = int((audit["event"] == "fast_path_decision").sum()) if not audit.empty else 0

        st.subheader("Throughput & cost")
        t1, t2, t3 = st.columns(3)
        t1.metric("Processed", f"{n:,}")
        t2.metric("Straight-through", f"{straight_through:.0%}", help="Paid or rejected with no human touch")
        t3.metric("Fast path", f"{fast / n:.0%}", help="Policy allowed only one outcome, so no LLM deliberation")
        t4, t5, t6 = st.columns(3)
        t4.metric("LLM calls / invoice", f"{df['llm_calls'].fillna(0).mean():.1f}")
        t5.metric("Agent time p50", f"{durations.quantile(0.5) / 1000:.2f}s" if len(durations) else "—")
        t6.metric("Agent time p95", f"{durations.quantile(0.95) / 1000:.2f}s" if len(durations) else "—")

        stats = JobQueue(store).stats()
        st.caption(f"Job queue: {stats.queued} queued · {stats.running} running · {stats.done} done · {stats.failed} failed")

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Invoices processed per minute**")
            per_min = pd.to_datetime(df["processed_at"]).dt.floor("min").value_counts().sort_index()
            st.bar_chart(per_min.rename("invoices"), height=220)
        with c2:
            st.markdown("**Most common findings** (errors and warnings)")
            codes = Counter()
            for rj in df["result_json"].dropna():
                for i in (json.loads(rj).get("validation") or {}).get("issues", []):
                    if i["severity"] != "info":
                        codes[i["code"]] += 1
            if codes:
                top = pd.Series(dict(codes.most_common(10)), name="invoices").sort_values()
                st.bar_chart(top, horizontal=True, height=220)
            else:
                st.caption("No findings yet.")

        if not audit.empty:
            st.markdown("**Where agent time goes** (per step)")
            audit["ms"] = audit["detail"].map(lambda d: json.loads(d).get("ms") if d else None)
            timed = audit.dropna(subset=["ms"])
            if not timed.empty:
                table = timed.groupby(["stage", "event"])["ms"].agg(count="count", p50=lambda x: x.quantile(0.5),
                                                                    p95=lambda x: x.quantile(0.95)).reset_index()
                st.dataframe(table.sort_values("p95", ascending=False), hide_index=True, width="stretch")

        st.subheader("Business impact estimate")
        st.caption("Edit the assumptions; the rates come from this ledger. Defaults derive from the brief: "
                   "$2M/yr lost to manual processing, 30% manual error rate.")
        a1, a2, a3 = st.columns(3)
        volume = a1.number_input("Invoices per year", 1_000, 1_000_000, 25_000, step=1_000)
        manual_cost = a2.number_input("Fully-loaded manual cost per invoice ($)", 1.0, 500.0, 80.0, step=5.0,
                                      help="$2M / 25,000 invoices = $80")
        review_share = a3.slider("Human effort on an escalated invoice vs fully manual", 0.1, 1.0, 0.25)
        blocked = df.loc[df["status"] == "rejected", "total"].clip(lower=0).sum()
        processed_value = df["total"].clip(lower=0).sum() or 1
        escalated = 1 - straight_through
        savings = volume * manual_cost * (straight_through + escalated * (1 - review_share))
        hours = volume * (straight_through + escalated * (1 - review_share)) * 15 / 60
        b1, b2, b3 = st.columns(3)
        b1.metric("Processing cost avoided / yr", money(savings))
        b2.metric("AP staff hours freed / yr", f"{hours:,.0f}", help="Assumes 15 minutes of manual work per invoice")
        b3.metric("Bad spend blocked (share of value seen)", f"{blocked / processed_value:.0%}", help=f"{money(blocked)} blocked so far")

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
                 if invoices else pd.DataFrame(), hide_index=True, width="stretch")
    st.subheader("Payments (mock bank API)")
    st.dataframe(pd.DataFrame(payments), hide_index=True, width="stretch")
    st.subheader("Inventory")
    st.dataframe(pd.DataFrame(store.list_inventory()), hide_index=True, width="stretch")
    with st.expander("Audit log (latest 500 events)"):
        st.dataframe(pd.DataFrame(store.list_audit()), hide_index=True, width="stretch")

with tab_about:
    st.markdown("""
#### Four agents, one graph
1. **Ingestion** — JSON/CSV/XML are parsed deterministically; free text and PDFs go to the LLM with a JSON schema.
   A **self-check** verifies every extracted value actually appears in the source and loops back with the specific problems (max 3 attempts).
2. **Validation** — code checks stock, catalog, arithmetic, dates, duplicates, vendor master, currency, **prompt injection**,
   **bank-detail changes** and **threshold splitting**. An **LLM risk analyst** adds the judgement calls rules miss.
3. **Approval** — a **VP reviewer** agent investigates with **tool calls** (policy, inventory, vendor master, history) and drafts a decision;
   an **internal-audit critic** challenges it and the reviewer revises (reflection loop). A **code guardrail** enforces the policy no matter what the LLMs say.
4. **Payment** — approved invoices hit the mock bank API (idempotently); rejections are logged with reasoning; escalations wait in the **review queue**.

#### Built for volume
- **Fast path**: when the policy allows only one outcome, no LLM deliberation is spent — most invoices need 0–2 model calls.
- **Concurrency + durable job queue**: worker pool, retries with backoff, crash recovery, a watch-folder inbox.
- **Race-safe ledger**: a write-time re-check plus database constraints mean two copies of one invoice can't both be paid.
- **MCP**: the ERP and bank run as MCP servers (`acme-erp`, `acme-bank`); the whole system is also an MCP server (`acme-ap`).
- **Learning**: human decisions become precedents the reviewer sees next time; repeated approvals of the same warning auto-resolve.
""")
    st.graphviz_chart("""
digraph G { rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#eef2ff", fontname="Helvetica"];
  load -> extract [label="free text"]; load -> self_check [label="structured"]; load -> failed [label="unreadable"];
  extract -> self_check; self_check -> extract [label="problems found", color="#d97706", fontcolor="#d97706"];
  self_check -> validate; validate -> policy_decision [label="one allowed outcome"]; policy_decision -> guardrail;
  validate -> review [label="discretion"]; review -> critique;
  critique -> review [label="audit disagrees", color="#d97706", fontcolor="#d97706"];
  critique -> guardrail [label="agrees / max rounds"]; guardrail -> payment;
  payment [fillcolor="#dcfce7"]; failed [fillcolor="#fee2e2"]; }
""")
