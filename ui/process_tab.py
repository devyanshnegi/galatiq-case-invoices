"""Process invoices: one at a time with a live agent trace, or the whole sample set in parallel."""

from __future__ import annotations

import contextlib
import io
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import streamlit as st

from invoice_agents.config import PROJECT_ROOT
from invoice_agents.graph import NODE_LABELS

from .common import findings, md, money, status_label

SAMPLES_DIR = PROJECT_ROOT / "data" / "invoices"
UPLOAD_DIR = PROJECT_ROOT / "uploads"
SUFFIXES = {".txt", ".pdf", ".json", ".csv", ".xml"}
NEXT_STEP = {"pending_review": "It's waiting in the **Review queue** tab.", "rejected": "It's in the **Rejected** tab, where it can be reopened if it's actually correct.",
             "paid": "See it in the **Payments** tab."}


def render_state(state: dict) -> None:
    inv = state.get("invoice") or {}
    status = state.get("status", "failed")
    if status == "failed":
        st.error(f"Could not process this document: {md(state.get('error'))}", icon="⚠️")
        return
    decision = state.get("decision") or {}
    validation = state.get("validation") or {}
    pol = state.get("policy") or {}

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Outcome", status_label(status), border=True)
    c2.metric("Amount", money(inv.get("total"), inv.get("currency", "USD")), border=True,
              help=f"≈ {money(validation.get('total_usd'))} USD" if inv.get("currency") != "USD" else None)
    c3.metric("Risk score", f"{validation.get('risk_score', 0)}/100", border=True)
    c4.metric("Approval tier", pol.get("tier", "—").upper(), border=True)

    box = {"paid": st.success, "rejected": st.error, "pending_review": st.warning}.get(status, st.info)
    box(f"**{decision.get('decision', '?').upper()}** — {md(decision.get('reasoning', ''))}  \n{NEXT_STEP.get(status, '')}")
    if decision.get("conditions"):
        st.markdown("**Conditions:** " + md(" · ".join(decision["conditions"])))

    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown("##### Extracted invoice")
        confidence = state.get("extraction_confidence")
        st.caption(f"{state.get('source_format', '?').upper()} · extracted via **{state.get('extraction_method')}**"
                   f" · {state.get('extraction_attempts') or 1} attempt(s)"
                   + (f" · confidence **{confidence:.0%}**" if confidence is not None else "")
                   + f" · {state.get('llm_calls', 0)} LLM call(s)")
        st.markdown(md(f"**{inv.get('invoice_number') or '?'}** from **{inv.get('vendor') or '❓ missing vendor'}**  \n"
                       f"Issued {inv.get('invoice_date') or '—'} · Due **{inv.get('due_date') or (inv.get('due_date_raw') or 'missing')}**"
                       f" · {inv.get('payment_terms') or 'no terms'}"))
        rows = [{"Item": li["item"], "Matched SKU": li.get("sku") or "❌ not in catalog", "Qty": li.get("quantity"),
                 "Unit price": li.get("unit_price"), "Discount": li.get("discount"), "Amount": li.get("amount"), "Note": li.get("note") or ""}
                for li in inv.get("line_items", [])]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        totals = {k: inv.get(k) for k in ("subtotal", "tax", "shipping", "total") if inv.get(k) is not None}
        st.caption(md(" · ".join(f"{k.title()}: {money(v, inv.get('currency', 'USD'))}" for k, v in totals.items())))
    with right:
        st.markdown("##### Validation findings")
        findings(validation.get("issues", []), include_info=True)

    critiques = state.get("critiques") or []
    with st.expander(f"🔁 Reflection loop — {len(critiques)} critique round(s)", expanded=len(critiques) > 1):
        if not critiques:
            st.caption("No deliberation needed: the policy allowed only one outcome (fast path).")
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


def _run_with_progress(pipeline, path: Path) -> dict:
    state: dict = {}
    with st.status(f"Processing {path.name}…", expanded=True) as status, contextlib.redirect_stdout(io.StringIO()):
        for node, update, state in pipeline.stream(path):
            ms = sum(e.get("ms") or 0 for e in update.get("trace") or [])
            st.write(f"✓ {NODE_LABELS.get(node, node)} · {ms} ms")
        status.update(label=f"{path.name}: {status_label(state.get('status'))}", state="complete", expanded=False)
    return state


def process_tab(pipeline, workers: int) -> None:
    store = pipeline.store
    samples = sorted(p for p in SAMPLES_DIR.iterdir() if p.suffix.lower() in SUFFIXES)
    with st.container(border=True):
        col_a, col_b = st.columns([2, 1], gap="large")
        with col_a:
            choice = st.selectbox("Sample invoice", samples, format_func=lambda p: p.name)
            upload = st.file_uploader("…or upload your own", type=[s.lstrip(".") for s in SUFFIXES])
        with col_b:
            run_one = st.button("Process invoice", type="primary", icon="▶️", width="stretch")
            run_all = st.button(f"Process all {len(samples)} samples", icon="⏩", width="stretch")
            fresh = st.checkbox("Start from a clean ledger", value=True,
                                help="Applies to both buttons. Clears processed invoices, payments, reviews and learned "
                                     "precedents first. Untick to keep history - re-processing an invoice is then "
                                     "(correctly) rejected as a duplicate.")

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
        st.session_state["last_state"] = _run_with_progress(pipeline, path)
        st.session_state.pop("batch", None)
        st.rerun()  # refresh the header counts and other tabs from the new ledger

    if run_all:
        progress = st.progress(0.0, text=f"Starting batch with {workers} workers…")
        results: dict[str, dict] = {}
        with contextlib.redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(pipeline.process, path): path for path in samples}
            for n, future in enumerate(as_completed(futures), start=1):  # UI updates stay on Streamlit's thread
                path = futures[future]
                results[path.name] = future.result()
                progress.progress(n / len(samples), text=f"{path.name}: {status_label(results[path.name].get('status'))}")
        st.session_state["batch"] = [(p.name, results[p.name]) for p in samples]
        st.session_state.pop("last_state", None)
        st.rerun()

    if st.session_state.get("batch"):
        batch = st.session_state["batch"]
        totals: dict[str, float] = {}
        for _, s in batch:
            usd = (s.get("validation") or {}).get("total_usd") or 0
            totals[s.get("status")] = totals.get(s.get("status"), 0) + max(usd, 0)
        m = st.columns(4)
        for col, label, key, suffix in ((m[0], "Paid automatically", "paid", "paid"), (m[1], "Rejected", "rejected", "blocked"),
                                        (m[2], "Needs a human", "pending_review", "waiting"), (m[3], "Failed", "failed", "")):
            col.metric(label, sum(1 for _, s in batch if s.get("status") == key), border=True)
            if suffix:
                col.caption(md(f"{money(totals.get(key, 0))} {suffix}"))
        table = []
        for name, s in batch:
            inv = s.get("invoice") or {}
            issues = [i for i in (s.get("validation") or {}).get("issues", []) if i["severity"] != "info"]
            table.append({"File": name, "Invoice": inv.get("invoice_number"), "Vendor": inv.get("vendor"),
                          "Amount": money(inv.get("total"), inv.get("currency", "USD")), "Outcome": status_label(s.get("status")),
                          "Why": issues[0]["message"] if issues else "All checks passed"})
        st.dataframe(pd.DataFrame(table), hide_index=True, width="stretch")
        pick = st.selectbox("Inspect one", [n for n, _ in batch])
        render_state(dict(batch)[pick])
    elif st.session_state.get("last_state"):
        render_state(st.session_state["last_state"])
