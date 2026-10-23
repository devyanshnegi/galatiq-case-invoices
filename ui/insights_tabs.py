"""Operations, Ledger & audit, and How it works."""

from __future__ import annotations

import json
from collections import Counter

import pandas as pd
import streamlit as st

from invoice_agents.db import Store
from invoice_agents.jobqueue import JobQueue

from .common import empty_state, money

HUMAN_PREFIXES = ("Approved by", "Rejected by", "Reopened by")


def operations_tab(store: Store) -> None:
    rows = store.list_invoices()
    if not rows:
        empty_state("📈", "No activity yet", "Process some invoices to see throughput, latency and business impact.")
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

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Invoices processed per minute**")
        per_min = pd.to_datetime(df["processed_at"]).dt.floor("min").value_counts().sort_index()
        st.bar_chart(per_min.rename("invoices"), height=220)
    with c2:
        st.markdown("**Most common findings** (errors and warnings)")
        codes: Counter = Counter()
        for rj in df["result_json"].dropna():
            for i in (json.loads(rj).get("validation") or {}).get("issues", []):
                if i["severity"] != "info":
                    codes[i["code"]] += 1
        if codes:
            st.bar_chart(pd.Series(dict(codes.most_common(10)), name="invoices").sort_values(), horizontal=True, height=220)
        else:
            st.caption("No findings yet.")

    if not audit.empty:
        audit["ms"] = audit["detail"].map(lambda d: json.loads(d).get("ms") if d else None)
        timed = audit.dropna(subset=["ms"])
        if not timed.empty:
            st.markdown("**Where agent time goes** (per step)")
            table = timed.groupby(["stage", "event"])["ms"].agg(count="count", p50=lambda x: x.quantile(0.5),
                                                                p95=lambda x: x.quantile(0.95)).reset_index()
            st.dataframe(table.sort_values("p95", ascending=False), hide_index=True, width="stretch")

    st.markdown("##### Business impact estimate")
    st.caption("Edit the assumptions; the rates come from this ledger. Defaults derive from the brief: "
               "$2M/yr lost to manual processing.")
    a = st.columns(3)
    volume = a[0].number_input("Invoices per year", 1_000, 1_000_000, 25_000, step=1_000)
    manual_cost = a[1].number_input("Fully-loaded manual cost per invoice ($)", 1.0, 500.0, 80.0, step=5.0, help="$2M / 25,000 invoices = $80")
    review_share = a[2].slider("Human effort on an escalated invoice vs fully manual", 0.1, 1.0, 0.25)
    blocked = df.loc[df["status"] == "rejected", "total"].clip(lower=0).sum()
    processed_value = df["total"].clip(lower=0).sum() or 1
    escalated = 1 - straight_through
    savings = volume * manual_cost * (straight_through + escalated * (1 - review_share))
    hours = volume * (straight_through + escalated * (1 - review_share)) * 15 / 60
    b = st.columns(3)
    b[0].metric("Processing cost avoided / yr", money(savings), border=True)
    b[1].metric("AP staff hours freed / yr", f"{hours:,.0f}", help="Assumes 15 minutes of manual work per invoice", border=True)
    b[2].metric("Bad spend blocked (share of value seen)", f"{blocked / processed_value:.0%}", help=f"{money(blocked)} blocked so far", border=True)


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
    st.graphviz_chart("""
digraph G { rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#eef2ff", fontname="Helvetica"];
  load -> extract [label="free text"]; load -> self_check [label="structured"]; load -> failed [label="unreadable"];
  extract -> self_check; self_check -> extract [label="problems found", color="#d97706", fontcolor="#d97706"];
  self_check -> validate; validate -> policy_decision [label="one allowed outcome"]; policy_decision -> guardrail;
  validate -> review [label="discretion"]; review -> critique;
  critique -> review [label="audit disagrees", color="#d97706", fontcolor="#d97706"];
  critique -> guardrail [label="agrees / max rounds"]; guardrail -> payment;
  payment -> human [label="escalated"]; human [label="human review\\n(1 or 2 approvers)", fillcolor="#fef3c7"];
  payment [fillcolor="#dcfce7"]; failed [fillcolor="#fee2e2"]; }
""")
