"""Payments: everything the bank has paid, who authorised it, and what's approved but not yet paid."""

from __future__ import annotations

from collections import defaultdict

import pandas as pd
import streamlit as st

from invoice_agents import review
from invoice_agents.db import Store

from .common import empty_state, md, money


def payments_tab(store: Store) -> None:
    payments = [p for p in store.list_payments() if p["status"] == "success"]
    invoices = {r["id"]: r for r in store.list_invoices()}
    approvers: dict[int, list[str]] = defaultdict(list)
    for a in reversed(store.all_approvals()):
        if a["action"] == "approve":
            approvers[a["invoice_id"]].append(f"{a['reviewer']} ({review.ROLES.get(a['role'], a['role'])})")

    human = [p for p in payments if approvers.get(p["invoice_id"])]
    dual = [p for p in human if len(approvers[p["invoice_id"]]) >= 2]
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Paid", f"{len(payments)} invoices", border=True)
    k2.metric("Total paid", money(sum(p["amount"] or 0 for p in payments if (p["currency"] or "USD") == "USD")), border=True,
              help="USD payments; foreign-currency payments are listed below in their own currency")
    k3.metric("Paid automatically", len(payments) - len(human), border=True, help="Clean invoices the policy let the agents pay")
    k4.metric("Human-approved", f"{len(human)} ({len(dual)} two-person)", border=True)

    stuck = [r for r in invoices.values() if r["status"] == "pending_review" and "bank declined" in (r["reasoning"] or "")]
    if stuck:
        st.warning(md(f"{len(stuck)} approved invoice(s) were declined by the bank (over its limit) and are back in the review queue: "
                      + ", ".join(r["invoice_number"] or str(r["id"]) for r in stuck)), icon="🏦")

    if not payments:
        empty_state("💸", "No payments yet", "Process invoices or approve escalations, and payments will appear here.")
        return

    rows = []
    for p in payments:
        inv = invoices.get(p["invoice_id"], {})
        who = approvers.get(p["invoice_id"])
        rows.append({
            "Paid at": (p["paid_at"] or "")[:16].replace("T", " "),
            "Reference": p["reference"],
            "Invoice": p["invoice_number"],
            "Vendor": p["vendor"],
            "Amount": p["amount"],
            "Currency": p["currency"],
            "Authorised by": " + ".join(who) if who else "Agents (policy auto-approval)",
            "Exception": "Yes" if inv.get("override_reason") else "",
        })
    df = pd.DataFrame(rows)
    st.dataframe(df, hide_index=True, width="stretch",
                 column_config={"Amount": st.column_config.NumberColumn(format="%.2f")})
    st.download_button("Download payment register (CSV)", df.to_csv(index=False).encode(), "payment_register.csv", "text/csv",
                       icon="⬇️")
