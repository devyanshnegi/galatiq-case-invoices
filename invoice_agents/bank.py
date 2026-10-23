"""The (mock) bank. Runs in-process, or behind the `acme-bank` MCP server - same code either way.

Controls live here, at the bank boundary, so they hold no matter which agent (or which buggy
model) asks for a payment: idempotency per invoice, and a single-payment limit.
"""

from __future__ import annotations

import sys
import uuid

from .db import Store


def mock_payment(vendor, amount):
    """The case's mock banking API, unchanged."""
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}


def pay(store: Store, *, invoice_id: int, invoice_number: str | None, vendor: str, amount: float, currency: str,
        single_payment_limit: float, dual_approved_limit: float | None = None, dual_approved: bool = False,
        quiet: bool = False) -> dict:
    if not vendor or amount is None or amount <= 0:
        return {"status": "declined", "reason": "missing payee or non-positive amount"}
    # Payments two humans signed off may go higher than what automation alone can release.
    limit = (dual_approved_limit or single_payment_limit) if dual_approved else single_payment_limit
    if amount > limit:
        kind = "dual-approved" if dual_approved else "single-payment"
        return {"status": "declined", "reason": f"{amount:,.2f} exceeds the bank's {kind} limit of {limit:,.2f}"}

    reference = f"PAY-{uuid.uuid4().hex[:10].upper()}"
    # Claim first, then move money: two workers (or a double click) can't both get past this line.
    if not store.claim_payment(invoice_id=invoice_id, invoice_number=invoice_number, vendor=vendor, amount=amount,
                               currency=currency, reference=reference):
        existing = store.payment_for_invoice(invoice_id) or {}
        return {"status": "skipped", "reason": "already paid", "reference": existing.get("reference")}
    try:
        if quiet:  # MCP stdio servers must keep stdout for the protocol
            stdout, sys.stdout = sys.stdout, sys.stderr
            try:
                response = mock_payment(vendor, amount)
            finally:
                sys.stdout = stdout
        else:
            response = mock_payment(vendor, amount)
    except Exception as exc:
        store.finish_payment(reference, "error")
        return {"status": "error", "reason": str(exc)}
    status = response.get("status", "unknown")
    store.finish_payment(reference, status)
    return {"status": status, "reference": reference, "amount": amount, "currency": currency, "vendor": vendor}
