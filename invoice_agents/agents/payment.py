"""Payment Agent: executes approved invoices through the bank backend and records every outcome."""

from __future__ import annotations

import json
import logging
import time

from ..bank import mock_payment  # noqa: F401  (re-exported: the case's API lives in invoice_agents.bank)
from ..checks import check_duplicates, check_threshold_splitting, fingerprint
from ..db import DuplicateRecord, Store
from ..models import Invoice, Severity
from ..state import AgentContext, InvoiceState, trace_event

log = logging.getLogger(__name__)

STATUS_FOR_DECISION = {"approve": "paid", "reject": "rejected", "escalate": "pending_review"}


def execute_payment(backend, invoice_id: int, inv: Invoice) -> dict:
    return backend.pay(invoice_id=invoice_id, invoice_number=inv.invoice_number, vendor=inv.vendor,
                       amount=inv.total, currency=inv.currency)


def payment_node(ctx: AgentContext):
    def pay(state: InvoiceState) -> InvoiceState:
        started = time.perf_counter()
        inv = Invoice(**state["invoice"])
        decision = dict(state["decision"])
        status = STATUS_FOR_DECISION[decision["decision"]]
        events = []
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
                    decision.update(decision="reject" if blocking else "escalate", override="ledger re-check at write time",
                                    reasoning=f"{decision['reasoning']} [Ledger re-check: " + "; ".join(i.message for i in late) + "]")
                    events.append(trace_event("payment", "late_duplicate_check", None, codes=[i.code for i in late], outcome=status))
                record = dict(run_id=state["run_id"], invoice=inv.model_dump(), fingerprint=fingerprint(inv),
                              source_file=state["invoice_path"], result={**_summary(state), "decision": decision})
                # Approved invoices are recorded as 'approved' and only become 'paid' once the bank confirms,
                # so a crash in between leaves an honest ledger and a resumable payment.
                initial = "approved" if status == "paid" else status
                invoice_id = ctx.store.record_invoice(status=initial, decision=decision["decision"], reasoning=decision["reasoning"], **record)
        except DuplicateRecord:  # backstop for multiple processes sharing one database
            # Another worker recorded this exact invoice between our duplicate check and now.
            status = "rejected"
            decision.update(decision="reject", override="database duplicate guard",
                            reasoning=f"{inv.invoice_number} is a duplicate: an identical invoice was recorded concurrently by another worker.")
            invoice_id = ctx.store.record_invoice(status=status, decision="reject", reasoning=decision["reasoning"],
                                                  **{**record, "fingerprint": None})
        if status == "paid":
            payment = execute_payment(ctx.backend, invoice_id, inv)
            if payment["status"] in ("success", "skipped"):
                ctx.store.update_invoice_status(invoice_id, "paid", decision["decision"], decision["reasoning"])
            elif payment["status"] == "declined":
                status = "pending_review"
                ctx.store.update_invoice_status(invoice_id, status, "escalate", f"Bank declined: {payment['reason']}")
            elif payment["status"] not in ("success", "skipped"):
                status = "failed"
                ctx.store.update_invoice_status(invoice_id, status, decision["decision"], f"Payment failed: {payment}")
            events.append(trace_event("payment", "payment_executed", started, invoice_id=invoice_id, backend=ctx.backend.name, **payment))
        elif status == "rejected":
            payment = {"status": "not_paid", "reason": decision["reasoning"]}
            events.append(trace_event("payment", "rejection_logged", started, invoice_id=invoice_id, **payment))
        else:
            payment = {"status": "on_hold", "reason": "awaiting human approval"}
            events.append(trace_event("payment", "queued_for_human_review", started, invoice_id=invoice_id, **payment))
        return {"status": status, "payment": payment, "invoice_id": invoice_id, "decision": decision, "trace": events}

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


def resolve_pending(store: Store, invoice_id: int, approve: bool, reviewer: str, note: str = "", backend=None) -> dict:
    """Human-in-the-loop: a person approves or rejects an escalated invoice.

    The decision is also remembered as a precedent for this vendor, which the reviewer agent sees
    (via the get_vendor_precedents tool and its case file) on the vendor's next escalated invoice.
    """
    from ..backends import LocalBackend
    from ..config import load_settings

    row = store.get_invoice(invoice_id)
    if row is None or row["status"] != "pending_review":
        raise ValueError(f"Invoice {invoice_id} is not awaiting review")
    backend = backend or LocalBackend(load_settings(db_path=store.path), store)

    result = json.loads(row["result_json"])
    inv = Invoice(**result["invoice"])
    reasoning = f"{'Approved' if approve else 'Rejected'} by {reviewer}" + (f": {note}" if note else "") + f" (agent said: {row['reasoning']})"
    if approve:
        payment = execute_payment(backend, invoice_id, inv)
        new_status = "paid" if payment["status"] in ("success", "skipped") else "pending_review" if payment["status"] == "declined" else "failed"
        store.update_invoice_status(invoice_id, new_status, "approve", reasoning if new_status == "paid" else f"{reasoning} - {payment}")
    else:
        payment = {"status": "not_paid"}
        store.update_invoice_status(invoice_id, "rejected", "reject", reasoning)
    warnings = [i["code"] for i in (result.get("validation") or {}).get("issues", []) if i["severity"] == "warning"]
    store.remember_human_decision(vendor=inv.vendor, invoice_number=inv.invoice_number, issue_codes=warnings,
                                  decision="approve" if approve else "reject", note=note, reviewer=reviewer)
    store.audit(run_id=row["run_id"], invoice_number=inv.invoice_number, stage="human_review",
                event="approved" if approve else "rejected", detail={"reviewer": reviewer, "note": note, "payment": payment})
    return payment


def _summary(state: InvoiceState) -> dict:
    keys = ("invoice", "validation", "risk", "policy", "decision", "critiques", "extraction_method", "source_format", "extraction_confidence")
    return {k: state.get(k) for k in keys}
