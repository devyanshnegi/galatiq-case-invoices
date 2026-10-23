"""Review queue (approve / reject / hold, two-person rule) and Rejected invoices (reopen as a policy exception)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from invoice_agents import review
from invoice_agents.config import Settings
from invoice_agents.db import Store

from .common import badge_md, empty_state, findings, md, money, status_badge_md


def _act(fn, **kwargs) -> None:
    """Run a review action, toast the result, refresh the page."""
    try:
        outcome = fn(**kwargs)
    except review.ReviewError as exc:
        st.toast(f"Not allowed: {exc}", icon="🚫")
        return
    icon = {"paid": "💸", "rejected": "⛔", "on_hold": "⏸️", "pending_review": "🔁", "awaiting_approval": "✍️"}.get(outcome["status"], "ℹ️")
    st.session_state["review_flash"] = (icon, outcome["message"])
    st.rerun()


def _flash() -> None:
    if msg := st.session_state.pop("review_flash", None):
        st.toast(msg[1], icon=msg[0])


def _header(case: review.ReviewCase) -> None:
    inv, row = case.invoice, case.row
    left, right = st.columns([3, 1])
    with left:
        st.markdown(f"#### {inv.invoice_number or 'No number'} · {md(inv.vendor or 'Unknown vendor')}")
        tags = [status_badge_md(case.status)]
        if case.requirement.approvals >= 2:
            tags.append(badge_md(f"{case.requirement.approvals} approvers needed", "violet"))
        if case.is_override:
            tags.append(badge_md("Policy exception", "red"))
        tier = (case.result.get("policy") or {}).get("tier")
        if tier:
            tags.append(badge_md(f"{tier.upper()} tier", "gray"))
        st.markdown(" ".join(tags))
    with right:
        st.metric("Amount", money(inv.total, inv.currency), help=f"≈ {money(case.amount_usd)} USD" if inv.currency != "USD" else None,
                  label_visibility="collapsed")
        st.caption(f"Due {inv.due_date or 'n/a'} · ID {row['id']}")


def _history(case: review.ReviewCase) -> None:
    if not case.history:
        return
    st.markdown("**Activity**")
    verbs = {"approve": "✍️ approved", "reject": "⛔ rejected", "hold": "⏸️ put on hold", "reopen": "🔁 reopened"}
    for h in case.history:
        note = f" — _{md(h['note'])}_" if h.get("note") else ""
        st.markdown(f"- {h['decided_at'][:16].replace('T', ' ')} · **{md(h['reviewer'])}** "
                    f"({review.ROLES.get(h['role'], h['role'])}) {verbs.get(h['action'], h['action'])}{note}")


def _document(case: review.ReviewCase) -> None:
    with st.expander("📄 Document and extracted data"):
        inv = case.invoice
        lines = pd.DataFrame([{"Item": li.item, "SKU": li.sku or "❌ not in catalog", "Qty": li.quantity, "Unit price": li.unit_price,
                               "Discount": li.discount, "Amount": li.amount, "Note": li.note or ""} for li in inv.line_items])
        if not lines.empty:
            st.dataframe(lines, hide_index=True, width="stretch")
        st.caption(md(" · ".join(f"{k.title()}: {money(getattr(inv, k), inv.currency)}"
                                 for k in ("subtotal", "tax", "shipping", "total") if getattr(inv, k) is not None)
                      + f" · Terms: {inv.payment_terms or 'n/a'} · Source: {case.row['source_file'] or 'n/a'}"))
        decision = case.result.get("decision") or {}
        if decision.get("conditions"):
            st.markdown("**Agent conditions:** " + md("; ".join(decision["conditions"])))


def _action_panel(case: review.ReviewCase, store: Store, settings: Settings, pipeline, reviewer: str, role: str) -> None:
    req = case.requirement
    done = len(case.approvers)
    st.markdown(f"**Approvals {done} / {req.approvals}**")
    st.progress(min(done / req.approvals, 1.0))
    for reason in req.reasons:
        st.caption(f"• {md(reason)}")
    if case.missing():
        st.caption("Still needed: " + md("; ".join(case.missing())))

    blocked = review.why_cannot_approve(case, reviewer, role, settings.policy)
    completes = blocked is None and review.would_complete(case, role)
    note = st.text_input("Note", key=f"note-{case.invoice_id}", placeholder="e.g. FX rate confirmed with treasury",
                         label_visibility="collapsed")
    label = "Approve & pay" if completes else f"Approve ({done + 1} of {req.approvals})"
    if st.button(label, key=f"approve-{case.invoice_id}", type="primary", icon="💸" if completes else "✍️",
                 disabled=blocked is not None, width="stretch"):
        _act(review.approve, store=store, invoice_id=case.invoice_id, reviewer=reviewer, role=role, note=note,
             backend=pipeline.backend, settings=settings)
    if blocked:
        st.caption(f"🔒 {md(blocked)}")

    c1, c2 = st.columns(2)
    with c1.popover("Hold", icon="⏸️", width="stretch", disabled=case.status != "pending_review"):
        why = st.text_area("What needs investigating?", key=f"hold-{case.invoice_id}",
                           placeholder="e.g. Calling vendor on file number to confirm bank change")
        if st.button("Put on hold", key=f"hold-btn-{case.invoice_id}", disabled=not why.strip()):
            _act(review.hold, store=store, invoice_id=case.invoice_id, reviewer=reviewer, role=role, note=why, settings=settings)
    with c2.popover("Reject", icon="⛔", width="stretch"):
        why = st.text_area("Reason for rejecting", key=f"reject-{case.invoice_id}")
        if st.button("Reject invoice", key=f"reject-btn-{case.invoice_id}", type="primary", disabled=not why.strip()):
            _act(review.reject, store=store, invoice_id=case.invoice_id, reviewer=reviewer, role=role, note=why, settings=settings)


def review_queue(store: Store, settings: Settings, pipeline, reviewer: str, role: str) -> None:
    _flash()
    cases = review.list_cases(store, settings.policy)
    if not cases:
        empty_state("🎉", "Nothing waiting for review", "Every processed invoice was resolved. Escalations will appear here.")
        return

    groups = {
        "All": cases,
        "Ready for me": [c for c in cases if review.why_cannot_approve(c, reviewer, role, settings.policy) is None],
        "Awaiting 2nd approval": [c for c in cases if c.approvers],
        "On hold": [c for c in cases if c.status == "on_hold"],
        "Exceptions": [c for c in cases if c.is_override],
    }
    pick = st.segmented_control("Show", [f"{k} · {len(v)}" for k, v in groups.items()], default=f"All · {len(cases)}",
                                label_visibility="collapsed") or f"All · {len(cases)}"
    shown = groups[pick.split(" · ")[0]]
    if not shown:
        empty_state("🔎", "Nothing in this view", "Try another filter.")
    for case in shown:
        with st.container(border=True):
            _header(case)
            st.warning(md(case.row["reasoning"] or ""), icon="🧭")
            left, right = st.columns([3, 2], gap="large")
            with left:
                findings((case.result.get("validation") or {}).get("issues", []))
                precedents = store.vendor_precedents(case.invoice.vendor, limit=3)
                if precedents:
                    st.info("📚 **Earlier human decisions for this vendor:** " + md(" · ".join(
                        f"{p['decision']} {p['invoice_number']} by {p['reviewer']} ({p['decided_at'][:10]})" for p in precedents)))
                _history(case)
            with right:
                _action_panel(case, store, settings, pipeline, reviewer, role)
            _document(case)


def rejected_tab(store: Store, settings: Settings, reviewer: str, role: str) -> None:
    _flash()
    cases = list(reversed(review.list_cases(store, settings.policy, statuses=("rejected",))))
    if not cases:
        empty_state("🧹", "No rejected invoices", "Invoices the agents or reviewers reject appear here, with the option to reopen them.")
        return
    st.caption("Rejected invoices are never paid automatically. If you believe one is actually correct, **reopen** it with a "
               "justification: it goes back to the review queue as a policy exception, and payment then needs "
               f"{settings.policy.override_approvals} approvers including the "
               f"{review.ROLES.get(settings.policy.override_required_role, settings.policy.override_required_role)}.")
    c1, c2 = st.columns([2, 3])
    query = c1.text_input("Search", placeholder="Vendor or invoice number", label_visibility="collapsed")
    who = c2.segmented_control("Rejected by", ["Everyone", "Agents", "People"], default="Everyone", label_visibility="collapsed") or "Everyone"
    if query:
        q = query.lower()
        cases = [c for c in cases if q in (c.invoice.vendor or "").lower() or q in (c.invoice.invoice_number or "").lower()]
    if who != "Everyone":
        human = lambda c: (c.row["reasoning"] or "").startswith("Rejected by")  # noqa: E731
        cases = [c for c in cases if human(c) == (who == "People")]
    st.caption(f"{len(cases)} invoice(s)")

    for case in cases:
        with st.container(border=True):
            _header(case)
            st.error(md(case.row["reasoning"] or ""), icon="⛔")
            left, right = st.columns([3, 2], gap="large")
            with left:
                findings((case.result.get("validation") or {}).get("issues", []))
                _history(case)
            with right:
                blocked = review.why_cannot_reopen(case, settings.policy)
                if blocked:
                    st.caption(f"🔒 Can't be reopened: {md(blocked)}")
                else:
                    with st.popover("Reopen for human review", icon="🔁", width="stretch"):
                        st.markdown("Explain why this invoice is actually correct. This is recorded in the audit trail.")
                        why = st.text_area("Justification", key=f"reopen-{case.invoice_id}",
                                           placeholder="e.g. Vendor confirmed by phone that stock was delivered from their second warehouse")
                        if st.button("Reopen", key=f"reopen-btn-{case.invoice_id}", type="primary", disabled=len(why.strip()) < 10):
                            _act(review.reopen, store=store, invoice_id=case.invoice_id, reviewer=reviewer, role=role, reason=why,
                                 settings=settings)
            _document(case)
