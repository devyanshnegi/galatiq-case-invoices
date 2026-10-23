"""Payment Agent: executes approved invoices through the bank backend and records every outcome."""

from __future__ import annotations

import logging
import time

from ..bank import mock_payment  # noqa: F401  (re-exported: the case's API lives in invoice_agents.bank)
from ..checks import check_duplicates, check_threshold_splitting, fingerprint
from ..db import DuplicateRecord, Store
from ..models import Invoice, Severity
from ..state import AgentContext, InvoiceState, trace_event

log = logging.getLogger(__name__)

STATUS_FOR_DECISION = {"approve": "paid", "reject": "rejected", "escalate": "pending_review"}


def amount_due(store: Store, inv: Invoice, invoice_id: int) -> tuple[float, float]:
    """(amount to pay now, already paid). A revision of an invoice that was already paid owes only the difference."""
    already = store.paid_against(inv.invoice_number, inv.vendor, invoice_id)
    return round((inv.total or 0) - already, 2), already


def execute_payment(store: Store, backend, invoice_id: int, inv: Invoice, dual_approved: bool = False) -> dict:
    due, already = amount_due(store, inv, invoice_id)
    if already and due <= 0:
        return {"status": "skipped", "reason": f"nothing further due: {already:,.2f} already paid under {inv.invoice_number}"}
    payment = backend.pay(invoice_id=invoice_id, invoice_number=inv.invoice_number, vendor=inv.vendor, amount=due,
                          currency=inv.currency, dual_approved=dual_approved)
    if already:
        payment["note"] = f"paid the difference: {inv.total:,.2f} less {already:,.2f} already paid under {inv.invoice_number}"
    return payment


def apply_payment_outcome(store: Store, invoice_id: int, payment: dict, decision: str, reasoning: str) -> str:
    """Write the ledger status for a payment attempt; returns it. Unknown outcomes are flagged, never assumed."""
    if payment["status"] in ("success", "skipped"):
        status = "paid"
        text = reasoning + (f" [{payment['note']}]" if payment.get("note") else "")
    elif payment["status"] == "declined":
        status, decision, text = "pending_review", "escalate", f"{reasoning} - bank declined: {payment.get('reason')}"
    elif payment["status"] == "unknown":
        status, text = "failed", f"{reasoning} - PAYMENT OUTCOME UNKNOWN: {payment.get('reason')}. Run `python main.py reconcile`."
    else:
        status, text = "failed", f"{reasoning} - payment failed: {payment}"
    store.update_invoice_status(invoice_id, status, decision, text)
    return status


def settle_unconfirmed(store: Store, backend, older_than_s: float = 300) -> list[dict]:
    """After a crash, in any mode: finish payments for invoices left 'approved', and list unknown outcomes.

    'approved' with no bank claim -> the bank was never called: pay now (idempotent).
    'approved' with a successful payment -> the crash hit after paying: just mark it paid.
    An unknown-outcome claim -> left alone and reported: a person must check with the bank.
    """
    import json

    report = []
    found = store.unsettled(older_than_s)
    for row in found["approved_unpaid"]:
        inv = Invoice(**json.loads(row["result_json"])["invoice"])
        existing = store.payment_for_invoice(row["id"])
        if existing and existing["status"] == "success":
            store.update_invoice_status(row["id"], "paid", "approve", row["reasoning"])
            report.append({"invoice_id": row["id"], "invoice_number": inv.invoice_number, "outcome": "already paid - marked paid"})
            continue
        payment = execute_payment(store, backend, row["id"], inv)
        status = apply_payment_outcome(store, row["id"], payment, "approve", row["reasoning"])
        report.append({"invoice_id": row["id"], "invoice_number": inv.invoice_number, "outcome": status, "payment": payment})
    for p in found["unknown_payments"]:
        report.append({"invoice_id": p["invoice_id"], "invoice_number": p["invoice_number"], "outcome": "needs bank reconciliation",
                       "payment": p})
    return report


def payment_node(ctx: AgentContext):
    def pay(state: InvoiceState) -> InvoiceState:
        started = time.perf_counter()
        inv = Invoice(**state["invoice"])
        decision = dict(state["decision"])
        status = STATUS_FOR_DECISION[decision["decision"]]
        events = []
        validation = dict(state["validation"])
        try:
            with ctx.ledger_lock:
                # Re-check the ledger at write time: with parallel workers, another copy or version of this
                # invoice may have been recorded after our validation ran.
                seen = {i["code"] for i in state["validation"]["issues"]}
                late = [i for i in [*check_duplicates(inv, ctx.store), *check_threshold_splitting(inv, ctx.store, ctx.settings.policy)]
                        if i.severity != Severity.INFO and i.code not in seen]
                if late and status != "rejected":
                    blocking = any(i.severity == Severity.ERROR for i in late)
                    status = "rejected" if blocking else "pending_review"
                    found = "; ".join(i.message for i in late)
                    # Lead with what changed: a reviewer must not read "Approving..." on an escalated invoice.
                    decision.update(decision="reject" if blocking else "escalate", override="ledger re-check at write time",
                                    reasoning=f"{'Rejected' if blocking else 'Escalated'} by the ledger re-check at payment time: {found} "
                                              f"(before that, the agents recommended: {decision['reasoning']})")
                    validation["issues"] = [*validation["issues"], *[i.model_dump(mode="json") for i in late]]
                    events.append(trace_event("payment", "late_duplicate_check", None, codes=[i.code for i in late], outcome=status))
                record = dict(run_id=state["run_id"], invoice=inv.model_dump(), fingerprint=fingerprint(inv),
                              source_file=state["invoice_path"],
                              result={**_summary(state), "decision": decision, "validation": validation})
                # Approved invoices are recorded as 'approved' and only become 'paid' once the bank confirms,
                # so a crash in between leaves an honest ledger and a resumable payment.
                initial = "approved" if status == "paid" else status
                invoice_id = ctx.store.record_invoice(status=initial, decision=decision["decision"], reasoning=decision["reasoning"], **record)
        except DuplicateRecord:  # backstop for multiple processes sharing one database
            # Another copy of this exact invoice is already approved, paid or in review.
            status = "rejected"
            decision.update(decision="reject", override="database duplicate guard",
                            reasoning=f"{inv.invoice_number} is a duplicate: an identical invoice is already approved, paid or in review.")
            validation["issues"] = [*validation["issues"], {"code": "DUPLICATE_INVOICE", "severity": "error", "field": "invoice_number",
                                                            "message": decision["reasoning"]}]
            record["result"] = {**record["result"], "decision": decision, "validation": validation}
            invoice_id = ctx.store.record_invoice(status=status, decision="reject", reasoning=decision["reasoning"],
                                                  **{**record, "fingerprint": None})
        if status == "paid":
            payment = execute_payment(ctx.store, ctx.backend, invoice_id, inv)
            status = apply_payment_outcome(ctx.store, invoice_id, payment, decision["decision"], decision["reasoning"])
            events.append(trace_event("payment", "payment_executed", started, invoice_id=invoice_id, backend=ctx.backend.name, **payment))
        elif status == "rejected":
            payment = {"status": "not_paid", "reason": decision["reasoning"]}
            events.append(trace_event("payment", "rejection_logged", started, invoice_id=invoice_id, **payment))
        else:
            payment = {"status": "on_hold", "reason": "awaiting human approval"}
            events.append(trace_event("payment", "queued_for_human_review", started, invoice_id=invoice_id, **payment))
        return {"status": status, "payment": payment, "invoice_id": invoice_id, "decision": decision,
                "validation": record["result"]["validation"], "trace": events}

    return pay


def failure_node(ctx: AgentContext):
    """Documents we can't even read still get a ledger entry, so nothing silently disappears."""

    def fail(state: InvoiceState) -> InvoiceState:
        invoice = state.get("invoice") or {}
        invoice_id = ctx.store.record_invoice(
            run_id=state["run_id"], invoice=invoice, status="failed", decision=None, reasoning=state.get("error"),
            fingerprint=None, source_file=state["invoice_path"], result={"error": state.get("error")},
        )
        return {"status": "failed", "invoice_id": invoice_id, "trace": [trace_event("payment", "failed", None, error=state.get("error"))]}

    return fail


def resolve_pending(store: Store, invoice_id: int, approve: bool, reviewer: str, note: str = "", backend=None,
                    role: str = "vp_finance", settings=None) -> dict:
    """Approve or reject an escalated invoice. Kept for the CLI/MCP/tests; the rules live in invoice_agents.review.

    Approving records one approval. Payment happens only once the policy's requirement is met
    (e.g. two different people for large amounts), so the return value is the payment when paid,
    or {'status': 'awaiting_approval', ...} while another approver is still needed.
    """
    from .. import review
    from ..backends import LocalBackend
    from ..config import load_settings

    settings = settings or load_settings(db_path=store.path)
    if approve:
        backend = backend or LocalBackend(settings, store)
        outcome = review.approve(store, invoice_id, reviewer=reviewer, role=role, note=note, backend=backend, settings=settings)
        return outcome.get("payment") or {"status": outcome["status"], "message": outcome["message"]}
    review.reject(store, invoice_id, reviewer=reviewer, role=role, note=note, settings=settings)
    return {"status": "not_paid"}


def _summary(state: InvoiceState) -> dict:
    keys = ("invoice", "validation", "risk", "policy", "decision", "critiques", "extraction_method", "source_format", "extraction_confidence")
    return {k: state.get(k) for k in keys}
