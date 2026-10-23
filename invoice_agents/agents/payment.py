"""Payment Agent: executes approved invoices against the (mock) banking API and records every outcome."""

from __future__ import annotations

import json
import logging
import time
import uuid

from ..checks import fingerprint
from ..db import Store
from ..models import Invoice
from ..state import AgentContext, InvoiceState, trace_event

log = logging.getLogger(__name__)

STATUS_FOR_DECISION = {"approve": "paid", "reject": "rejected", "escalate": "pending_review"}


def mock_payment(vendor, amount):
    """The case's mock banking API, unchanged."""
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}


def execute_payment(store: Store, invoice_id: int, inv: Invoice) -> dict:
    """Idempotent: an invoice row is paid at most once, however many times this is called."""
    if store.is_paid(invoice_id):
        return {"status": "skipped", "reason": "already paid"}
    response = mock_payment(inv.vendor, inv.total)
    reference = f"PAY-{uuid.uuid4().hex[:10].upper()}"
    status = response.get("status", "unknown")
    store.record_payment(invoice_id=invoice_id, invoice_number=inv.invoice_number, vendor=inv.vendor, amount=inv.total,
                         currency=inv.currency, reference=reference, status=status)
    return {"status": status, "reference": reference, "amount": inv.total, "currency": inv.currency, "vendor": inv.vendor}


def payment_node(ctx: AgentContext):
    def pay(state: InvoiceState) -> InvoiceState:
        started = time.perf_counter()
        inv = Invoice(**state["invoice"])
        decision = state["decision"]
        status = STATUS_FOR_DECISION[decision["decision"]]
        invoice_id = ctx.store.record_invoice(
            run_id=state["run_id"], invoice=inv.model_dump(), status=status, decision=decision["decision"],
            reasoning=decision["reasoning"], fingerprint=fingerprint(inv), source_file=state["invoice_path"],
            result=_summary(state),
        )
        if status == "paid":
            payment = execute_payment(ctx.store, invoice_id, inv)
            if payment["status"] != "success":
                status = "failed"
                ctx.store.update_invoice_status(invoice_id, status, decision["decision"], f"Payment failed: {payment}")
            event = "payment_executed"
        elif status == "rejected":
            payment = {"status": "not_paid", "reason": decision["reasoning"]}
            event = "rejection_logged"
        else:
            payment = {"status": "on_hold", "reason": "awaiting human approval"}
            event = "queued_for_human_review"
        return {"status": status, "payment": payment, "invoice_id": invoice_id,
                "trace": [trace_event("payment", event, started, invoice_id=invoice_id, **payment)]}

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


def resolve_pending(store: Store, invoice_id: int, approve: bool, reviewer: str, note: str = "") -> dict:
    """Human-in-the-loop: a person approves or rejects an escalated invoice from the dashboard."""
    row = store.get_invoice(invoice_id)
    if row is None or row["status"] != "pending_review":
        raise ValueError(f"Invoice {invoice_id} is not awaiting review")

    inv = Invoice(**json.loads(row["result_json"])["invoice"])
    reasoning = f"{'Approved' if approve else 'Rejected'} by {reviewer}" + (f": {note}" if note else "") + f" (agent said: {row['reasoning']})"
    if approve:
        payment = execute_payment(store, invoice_id, inv)
        store.update_invoice_status(invoice_id, "paid" if payment["status"] == "success" else "failed", "approve", reasoning)
    else:
        payment = {"status": "not_paid"}
        store.update_invoice_status(invoice_id, "rejected", "reject", reasoning)
    store.audit(run_id=row["run_id"], invoice_number=inv.invoice_number, stage="human_review",
                event="approved" if approve else "rejected", detail={"reviewer": reviewer, "note": note, "payment": payment})
    return payment


def _summary(state: InvoiceState) -> dict:
    return {k: state.get(k) for k in ("invoice", "validation", "risk", "policy", "decision", "critiques", "extraction_method", "source_format")}
