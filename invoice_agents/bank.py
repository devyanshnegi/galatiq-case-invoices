"""The (mock) bank. Runs in-process, or behind the `acme-bank` MCP server - same code either way.

Controls live here, at the bank boundary, so they hold no matter which agent (or which buggy
model) asks for a payment:

* one payment per invoice (the slot is claimed in the database *before* money moves);
* limits in USD, whatever the invoice currency - a 49,000 EUR invoice is ~$52,900, not "49,000";
* an attempt whose outcome is unknown (crash, timeout, unclear bank answer) keeps its slot locked,
  so a retry can never pay twice. Someone reconciles it with the bank first.
"""

from __future__ import annotations

import sys
import uuid

from .db import Store

# Bank answers that mean "no money moved" - the slot is released so the payment can be retried.
DEFINITE_FAILURES = {"declined", "failed", "rejected", "insufficient_funds"}


def mock_payment(vendor, amount):
    """The case's mock banking API, unchanged."""
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}


def pay(store: Store, *, invoice_id: int, invoice_number: str | None, vendor: str, amount: float, currency: str,
        single_payment_limit: float, dual_approved_limit: float | None = None, dual_approved: bool = False,
        fx_rates: dict[str, float] | None = None, quiet: bool = False) -> dict:
    if not vendor or amount is None or amount <= 0:
        return {"status": "declined", "reason": "missing payee or non-positive amount"}
    rates = fx_rates or {"USD": 1.0}
    rate = rates.get((currency or "USD").upper())
    if rate is None:
        return {"status": "declined", "reason": f"the bank has no rate for {currency}"}
    amount_usd = round(amount * rate, 2)
    # Payments two humans signed off may go higher than what automation alone can release.
    limit = (dual_approved_limit or single_payment_limit) if dual_approved else single_payment_limit
    if amount_usd > limit:
        kind = "dual-approved" if dual_approved else "single-payment"
        shown = f"${amount_usd:,.2f}" + ("" if (currency or "USD").upper() == "USD" else f" ({amount:,.2f} {currency})")
        return {"status": "declined", "reason": f"{shown} exceeds the bank's {kind} limit of ${limit:,.2f}"}

    reference = f"PAY-{uuid.uuid4().hex[:10].upper()}"
    # Claim first, then move money: two workers (or a double click) can't both get past this line.
    if not store.claim_payment(invoice_id=invoice_id, invoice_number=invoice_number, vendor=vendor, amount=amount,
                               currency=currency, reference=reference):
        existing = store.payment_for_invoice(invoice_id) or {}
        if existing.get("status") == "success":
            return {"status": "skipped", "reason": "already paid", "reference": existing.get("reference")}
        return {"status": "unknown", "reference": existing.get("reference"),
                "reason": f"an earlier payment attempt ({existing.get('reference')}) has an unconfirmed outcome "
                          f"('{existing.get('status')}') - reconcile it with the bank before retrying"}
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
        # The call may or may not have reached the bank: never assume nothing moved.
        store.finish_payment(reference, "unknown")
        return {"status": "unknown", "reference": reference, "reason": f"bank call failed mid-flight: {exc}"}
    status = str((response or {}).get("status", "unknown")).lower()
    if status != "success" and status not in DEFINITE_FAILURES:
        status = "unknown"
    store.finish_payment(reference, status)
    out = {"status": status, "reference": reference, "amount": amount, "currency": currency, "vendor": vendor}
    if status != "success":
        out["reason"] = "the bank declined the payment" if status in DEFINITE_FAILURES else "the bank's answer was unclear - reconcile before retrying"
    return out
