"""Operations, Ledger & audit, and How it works."""

from __future__ import annotations

import json
from collections import Counter

import altair as alt
import pandas as pd
import streamlit as st

from invoice_agents.db import Store
from invoice_agents.jobqueue import JobQueue

from .common import empty_state

HUMAN_PREFIXES = ("Approved by", "Rejected by", "Reopened by")

# Trace events -> what a finance reader would call the step.
STEP_LABELS = {
    "document_loaded": "Open the document",
    "extracted": "Read the invoice",
    "self_check": "Self-check vs document",
    "rule_checks": "Rule checks (stock, maths, fraud)",
    "risk_review": "Fraud check (AI)",
    "risk_review_failed": "Fraud check (AI)",
    "risk_review_skipped": "Fraud check (AI)",
    "fast_path_decision": "Fast-path decision",
    "draft_decision": "Reviewer decision (AI)",
    "critique": "Audit critique (AI)",
    "payment_executed": "Record and pay",
    "rejection_logged": "Record and pay",
    "queued_for_human_review": "Record and pay",
}
BAR_COLOR = "#4C78A8"  # one series per chart: a single calm hue, readable on light and dark backgrounds


def _bars(data: pd.DataFrame, label: str, value: str, axis_title: str, fmt: str = "d") -> alt.LayerChart:
    """Horizontal bars, longest first, full labels on the left and the value at the end of each bar."""
    order = alt.SortField(value, order="descending")
    base = alt.Chart(data).encode(
        # labelOverlap=False: Vega otherwise hides labels it thinks are too close, which dropped rows on short charts.
        y=alt.Y(f"{label}:N", sort=order, title=None,
                axis=alt.Axis(labelLimit=320, labelFontSize=12, labelOverlap=False, labelPadding=6, ticks=False, domain=False)),
        x=alt.X(f"{value}:Q", title=axis_title, axis=alt.Axis(grid=True, gridOpacity=0.15, tickCount=4, format=fmt)),
        tooltip=[alt.Tooltip(f"{label}:N", title=label.capitalize()), alt.Tooltip(f"{value}:Q", title=axis_title, format=fmt)],
    )
    bars = base.mark_bar(color=BAR_COLOR, cornerRadiusEnd=4, height={"band": 0.7})
    # Value labels in a mid grey: readable on both light and dark themes (the default black vanishes on dark).
    labels = base.mark_text(align="left", dx=4, fontSize=12, color="#8B95A5").encode(text=alt.Text(f"{value}:Q", format=fmt))
    return (bars + labels).properties(height=40 + 32 * len(data))  # room for every row plus the x axis


def operations_tab(store: Store) -> None:
    rows = store.list_invoices()
    if not rows:
        empty_state("📈", "No activity yet", "Process some invoices to see throughput, latency and the most common findings.")
        return
    df = pd.DataFrame(rows)
    n = len(df)
    human = df["reasoning"].fillna("").str.startswith(HUMAN_PREFIXES)
    straight_through = ((df["status"].isin(["paid", "rejected"])) & ~human).sum() / n
    durations = df["duration_ms"].dropna()
    audit = pd.DataFrame(store.list_audit(limit=20000))
    fast = int((audit["event"] == "fast_path_decision").sum()) if not audit.empty else 0

    st.markdown("##### Throughput & cost")
    t = st.columns(3)
    t[0].metric("Processed", f"{n:,}", border=True)
    t[1].metric("Straight-through", f"{straight_through:.0%}", help="Paid or rejected with no human touch", border=True)
    t[2].metric("Fast path", f"{fast / n:.0%}", help="Policy allowed only one outcome, so no LLM deliberation", border=True)
    t = st.columns(3)
    t[0].metric("LLM calls / invoice", f"{df['llm_calls'].fillna(0).mean():.1f}", border=True)
    t[1].metric("Agent time p50", f"{durations.quantile(0.5) / 1000:.2f}s" if len(durations) else "—", border=True)
    t[2].metric("Agent time p95", f"{durations.quantile(0.95) / 1000:.2f}s" if len(durations) else "—", border=True)
    stats = JobQueue(store).stats()
    st.caption(f"Job queue: {stats.queued} queued · {stats.running} running · {stats.done} done · {stats.failed} failed")

    timed = pd.DataFrame()
    if not audit.empty:
        audit["ms"] = audit["detail"].map(lambda d: json.loads(d).get("ms") if d else None)
        timed = audit.dropna(subset=["ms"])

    c1, c2 = st.columns(2, gap="large")
    with c1:
        st.markdown("**Where the time goes**")
        if timed.empty:
            st.caption("No timing data yet.")
        else:
            runs = max(timed["run_id"].nunique(), 1)
            steps = (timed.assign(step=timed["event"].map(lambda e: STEP_LABELS.get(e, e.replace("_", " ").capitalize())))
                     .groupby("step")["ms"].sum().div(1000 * runs).round(2).rename("seconds").reset_index())
            steps = steps[steps["seconds"] > 0]
            st.caption(f"Average seconds per invoice spent in each step, across {runs} invoice(s). "
                       f"Together ≈ {steps['seconds'].sum():.1f}s per invoice; AI steps dominate when a model is used.")
            st.altair_chart(_bars(steps, "step", "seconds", "Seconds per invoice", fmt=".2f"), width="stretch")
    with c2:
        st.markdown("**Most common findings**")
        codes: Counter = Counter()
        for rj in df["result_json"].dropna():
            for i in (json.loads(rj).get("validation") or {}).get("issues", []):
                if i["severity"] != "info":
                    codes[i["code"]] += 1
        if codes:
            st.caption(f"Errors and warnings across {n} invoice(s) - what most often stops or holds an invoice.")
            found = pd.DataFrame([{"finding": c.replace("_", " ").capitalize(), "invoices": v} for c, v in codes.most_common(12)])
            st.altair_chart(_bars(found, "finding", "invoices", "Invoices"), width="stretch")
        else:
            st.caption("No findings yet.")

    if not timed.empty:
        with st.expander("Step timings in detail (milliseconds per step)"):
            st.caption("p50 = median, p95 = the slowest 5% start here. Opening PDFs is slower than JSON/CSV/TXT, "
                       "so 'document loaded' has a long tail when PDFs are in the mix.")
            table = (timed.assign(step=timed["event"].map(lambda e: STEP_LABELS.get(e, e.replace("_", " ").capitalize())))
                     .groupby("step")["ms"].agg(runs="count", p50=lambda x: x.quantile(0.5), p95=lambda x: x.quantile(0.95),
                                                slowest="max").round(0).astype(int).reset_index())
            st.dataframe(table.sort_values("p95", ascending=False), hide_index=True, width="stretch",
                         column_config={"step": "Step", "runs": "Runs", "p50": "p50 (ms)", "p95": "p95 (ms)", "slowest": "Slowest (ms)"})


def ledger_tab(store: Store) -> None:
    invoices = store.list_invoices()
    st.markdown("##### Invoices")
    st.dataframe(pd.DataFrame(invoices, columns=["id", "invoice_number", "vendor", "total", "currency", "status", "decision",
                                                 "reasoning", "override_reason", "source_file", "processed_at"])
                 if invoices else pd.DataFrame(), hide_index=True, width="stretch")
    st.markdown("##### Human review actions")
    st.dataframe(pd.DataFrame(store.all_approvals()), hide_index=True, width="stretch")
    st.markdown("##### Inventory")
    st.dataframe(pd.DataFrame(store.list_inventory()), hide_index=True, width="stretch")
    with st.expander("Audit log (latest 500 events)"):
        st.dataframe(pd.DataFrame(store.list_audit()), hide_index=True, width="stretch")


def about_tab() -> None:
    st.markdown("""
#### Four agents, one graph
1. **Ingestion** — JSON/CSV/XML are parsed deterministically; free text and PDFs go to the LLM with a JSON schema.
   A **self-check** verifies every extracted value actually appears in the source and loops back with the specific problems.
2. **Validation** — code checks stock, catalog, arithmetic, dates, duplicates, vendor master, currency, **prompt injection**,
   **bank-detail changes** and **threshold splitting**. An **LLM risk analyst** adds the judgement calls rules miss.
3. **Approval** — a **VP reviewer** agent investigates with **tool calls** and drafts a decision; an **internal-audit critic**
   challenges it (reflection loop). A **code guardrail** enforces the policy no matter what the models say.
4. **Payment** — approved invoices hit the bank (idempotently); rejections are logged with reasoning; escalations go to people.

#### People stay in control
- **Review queue**: approve, reject, or put on hold for investigation. Each role has an approval limit.
- **Two-person rule**: large invoices and bank/remit-address changes need two different approvers, one of them senior,
  before any money moves. Dual-approved payments may go above the bank's automatic limit.
- **Rejected invoices** can be reopened with a written justification if a person believes they're correct — a policy
  exception that needs two approvers including the CFO. Duplicates and prompt-injection attacks can never be reopened.
- Every human action is in the audit trail and becomes a precedent the agents learn from.
""")
    # The same graph as the README's Workflow section - keep the two in step.
    st.graphviz_chart("""
digraph G { rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#eef2ff", fontname="Helvetica"];
  L [label="Load file"]; X [label="Extract with LLM"]; S [label="Self-check vs document"];
  FAIL [label="Logged as failed", fillcolor="#fee2e2"]; V [label="Validation"]; P [label="Fast-path decision"];
  R [label="VP reviewer"]; C [label="Critic"]; G [label="Policy guardrail"];
  PAY [label="Payment / rejection log /\nhuman review", fillcolor="#dcfce7"];
  L -> X [label="TXT / PDF"]; L -> S [label="JSON / CSV / XML"]; L -> FAIL [label="unreadable"];
  X -> S; S -> X [label="problems, attempts left", color="#d97706", fontcolor="#d97706"];
  S -> V; V -> P [label="only one outcome allowed"]; V -> R [label="needs judgement"];
  R -> C; C -> R [label="disagrees, rounds left", color="#d97706", fontcolor="#d97706"];
  C -> G; P -> G; G -> PAY; }
""")

