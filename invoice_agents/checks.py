"""Deterministic validation checks.

Anything with a right answer - stock levels, arithmetic, dates, duplicates - is checked in code,
never by the LLM. Each check is a pure function returning Issues, so they're trivially testable
and the same checks are exposed to the LLM agents as tools.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from collections import defaultdict
from datetime import date

from .config import PolicySettings
from .db import Store, normalize_name
from .models import Invoice, Issue, Severity, ValidationReport
from .normalize import parse_terms_days

E, W, I = Severity.ERROR, Severity.WARNING, Severity.INFO
MONEY_TOLERANCE = 0.01
TERMS_TOLERANCE_DAYS = 3

# Fallback FX rates; the policy file's currency.reference_rates_to_usd is what the checks use.
FX_TO_USD = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "CAD": 0.73}

FRAUD_PATTERNS = {
    r"\burgent\b": "urgency pressure ('URGENT')",
    r"pay\s+immediately|immediate\s+payment": "demands immediate payment",
    r"wire\s+transfer": "requests wire transfer",
    r"avoid\s+penalt": "threatens penalties",
    r"gift\s*card|bitcoin|crypto": "unusual payment method",
}
BANK_CHANGE_PATTERN = (r"new\s+(bank\s+)?account|bank\s+details\s+(have\s+)?(changed|been\s+updated)|updated\s+(bank|remittance|account)"
                       r"|remit\s+to\s+(our\s+)?new")


def _close(a: float, b: float, tol: float = MONEY_TOLERANCE) -> bool:
    return abs(a - b) <= tol


def fingerprint(inv: Invoice) -> str:
    """Identity of an invoice's *content*, used to tell an exact duplicate from a revision."""
    key = f"{normalize_name(inv.vendor)}|{inv.invoice_number}|{inv.total}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def to_usd(amount: float | None, currency: str, rates: dict[str, float] | None = None) -> float | None:
    rate = (rates or FX_TO_USD).get((currency or "USD").upper())
    return None if amount is None or rate is None else round(amount * rate, 2)


# ---- individual checks ----------------------------------------------------------------------


def check_required_fields(inv: Invoice) -> list[Issue]:
    issues = []
    if not inv.vendor:
        issues.append(Issue(code="MISSING_VENDOR", severity=E, field="vendor", message="Vendor name is missing - cannot identify who to pay."))
    if not inv.invoice_number:
        issues.append(Issue(code="MISSING_INVOICE_NUMBER", severity=W, field="invoice_number", message="No invoice number - duplicate detection is unreliable."))
    if not inv.line_items:
        issues.append(Issue(code="NO_LINE_ITEMS", severity=E, field="line_items", message="No line items found on the invoice."))
    if inv.total is None:
        issues.append(Issue(code="MISSING_TOTAL", severity=E, field="total", message="No total amount found."))
    if inv.due_date is None:
        if inv.due_date_raw and inv.due_date_raw.strip():
            issues.append(Issue(code="INVALID_DUE_DATE", severity=E, field="due_date",
                                message=f"Due date '{inv.due_date_raw}' is not a real calendar date."))
        else:
            issues.append(Issue(code="MISSING_DUE_DATE", severity=E, field="due_date", message="Due date is missing."))
    return issues


def check_line_items(inv: Invoice, store: Store, policy: PolicySettings) -> list[Issue]:
    issues: list[Issue] = []
    requested: dict[str, float] = defaultdict(float)
    lines_per_sku: dict[str, int] = defaultdict(int)
    catalog: dict[str, dict] = {}

    for n, li in enumerate(inv.line_items, start=1):
        label = f"line {n} ({li.item})"
        if li.quantity is None:
            issues.append(Issue(code="MISSING_QUANTITY", severity=E, field=f"line_items[{n}]", message=f"{label}: quantity missing."))
        elif li.quantity <= 0:
            issues.append(Issue(code="INVALID_QUANTITY", severity=E, field=f"line_items[{n}]",
                                message=f"{label}: quantity {li.quantity:g} is not positive - data integrity issue."))
        elif li.quantity != int(li.quantity):
            issues.append(Issue(code="FRACTIONAL_QUANTITY", severity=W, field=f"line_items[{n}]", message=f"{label}: fractional quantity {li.quantity:g}."))
        if li.unit_price is not None and li.unit_price < 0:
            issues.append(Issue(code="NEGATIVE_PRICE", severity=E, field=f"line_items[{n}]", message=f"{label}: negative unit price."))
        if li.unit_price is None and li.amount is None:
            issues.append(Issue(code="MISSING_PRICE", severity=E, field=f"line_items[{n}]",
                                message=f"{label}: no unit price or line amount - the total can't be verified."))

        row = store.match_item(li.item)
        if row is None:
            issues.append(Issue(code="UNKNOWN_ITEM", severity=E, field=f"line_items[{n}]",
                                message=f"{label}: '{li.item}' is not in the inventory catalog."))
            continue
        li.sku = row["item"]
        catalog[row["item"]] = row
        lines_per_sku[row["item"]] += 1
        if li.quantity and li.quantity > 0:
            requested[row["item"]] += li.quantity

        catalog_price = row["unit_price"] or 0
        # Use the stated unit price, or derive it from the line amount when only that is given.
        unit_price = li.unit_price
        if unit_price is None and li.amount is not None and li.quantity:
            unit_price = (li.amount + (li.discount or 0)) / li.quantity
        if unit_price is not None and catalog_price > 0 and inv.currency == policy.base_currency:
            delta = (unit_price - catalog_price) / catalog_price
            if delta > policy.price_variance_tolerance:
                issues.append(Issue(code="PRICE_ABOVE_CATALOG", severity=W, field=f"line_items[{n}]",
                                    message=f"{label}: ${unit_price:,.2f} is {delta:+.0%} vs catalog ${catalog_price:,.2f}"
                                            + (f" (invoice note: '{li.note}')" if li.note else "") + "."))
            elif delta < -policy.price_variance_tolerance:
                issues.append(Issue(code="PRICE_BELOW_CATALOG", severity=I, field=f"line_items[{n}]",
                                    message=f"{label}: ${unit_price:,.2f} is {delta:+.0%} vs catalog - discount."))

    for sku, qty in requested.items():
        row = catalog[sku]
        if row["stock"] <= 0:
            issues.append(Issue(code="OUT_OF_STOCK_ITEM", severity=E, field="line_items",
                                message=f"{sku} has zero stock / is not orderable ({row['description']}) - suspicious."))
        elif qty > row["stock"]:
            spread = f" across {lines_per_sku[sku]} lines" if lines_per_sku[sku] > 1 else ""
            issues.append(Issue(code="INSUFFICIENT_STOCK", severity=E, field="line_items",
                                message=f"{sku}: invoice bills {qty:g} units{spread} but only {row['stock']} on record."))
        elif lines_per_sku[sku] > 1:
            issues.append(Issue(code="SPLIT_LINES", severity=I, field="line_items",
                                message=f"{sku} appears on {lines_per_sku[sku]} lines ({qty:g} units total) - within stock."))
    return issues


def check_arithmetic(inv: Invoice) -> list[Issue]:
    issues: list[Issue] = []
    line_sum = 0.0
    computable = bool(inv.line_items)
    for n, li in enumerate(inv.line_items, start=1):
        if li.quantity is None or li.unit_price is None:
            if li.amount is None:
                computable = False  # MISSING_PRICE / MISSING_QUANTITY already block it
            else:
                line_sum += li.amount  # amount-only line: can't check the line, but it still counts toward the total
            continue
        discount = li.discount or 0.0
        expected = round(li.quantity * li.unit_price - discount, 2)
        if discount < 0 or (discount and discount > li.quantity * li.unit_price):
            issues.append(Issue(code="INVALID_DISCOUNT", severity=E, field=f"line_items[{n}]",
                                message=f"line {n} ({li.item}): discount ${discount:,.2f} is negative or exceeds the line value."))
        if li.amount is not None and not _close(li.amount, expected):
            less = f" - ${discount:,.2f} discount" if discount else ""
            issues.append(Issue(code="LINE_TOTAL_MISMATCH", severity=E, field=f"line_items[{n}]",
                                message=f"line {n} ({li.item}): {li.quantity:g} x ${li.unit_price:,.2f}{less} = ${expected:,.2f}, invoice says ${li.amount:,.2f}."))
        line_sum += li.amount if li.amount is not None else expected

    if computable and inv.subtotal is not None and not _close(line_sum, inv.subtotal):
        issues.append(Issue(code="SUBTOTAL_MISMATCH", severity=E, field="subtotal",
                            message=f"Line items sum to ${line_sum:,.2f} but subtotal says ${inv.subtotal:,.2f}."))
    if inv.tax_rate and inv.subtotal is not None and inv.tax is not None:
        expected_tax = round(inv.subtotal * inv.tax_rate, 2)
        if not _close(expected_tax, inv.tax, 0.05):
            issues.append(Issue(code="TAX_MISMATCH", severity=W, field="tax",
                                message=f"Tax {inv.tax_rate:.0%} of ${inv.subtotal:,.2f} is ${expected_tax:,.2f}, invoice says ${inv.tax:,.2f}."))
    base = inv.subtotal if inv.subtotal is not None else (line_sum if computable else None)
    if base is not None and inv.total is not None:
        expected_total = round(base + (inv.tax or 0) + (inv.shipping or 0), 2)
        if not _close(expected_total, inv.total):
            issues.append(Issue(code="TOTAL_MISMATCH", severity=E, field="total",
                                message=f"Subtotal + tax + shipping = ${expected_total:,.2f} but total says ${inv.total:,.2f}."))
    if inv.total is not None and inv.total <= 0:
        issues.append(Issue(code="NON_POSITIVE_TOTAL", severity=E, field="total", message=f"Total ${inv.total:,.2f} is not a payable amount."))
    return issues


def check_charges(inv: Invoice, policy: PolicySettings) -> list[Issue]:
    """Tax and freight that add up can still be inflated: compare them with what's plausible."""
    issues: list[Issue] = []
    goods = inv.subtotal if inv.subtotal is not None else sum(li.amount or 0 for li in inv.line_items) or None
    if not goods or goods <= 0:
        return issues
    implied = (inv.tax or 0) / goods
    rate = max(implied, inv.tax_rate or 0)
    if rate > policy.max_tax_rate + 1e-9:
        issues.append(Issue(code="TAX_RATE_UNUSUAL", severity=W, field="tax",
                            message=f"Tax is {rate:.1%} of the goods value (${inv.tax or 0:,.2f} on ${goods:,.2f}); "
                                    f"the policy maximum is {policy.max_tax_rate:.0%}."))
    if inv.shipping and inv.shipping / goods > policy.freight_tolerance + 1e-9:
        issues.append(Issue(code="FREIGHT_ABOVE_TOLERANCE", severity=W, field="shipping",
                            message=f"Shipping ${inv.shipping:,.2f} is {inv.shipping / goods:.1%} of the goods value; "
                                    f"tolerance is {policy.freight_tolerance:.0%}."))
    return issues


def check_dates(inv: Invoice, today: date | None = None, terms_tolerance_days: int = TERMS_TOLERANCE_DAYS) -> list[Issue]:
    issues: list[Issue] = []
    today = today or date.today()
    if not inv.invoice_date:
        issues.append(Issue(code="MISSING_INVOICE_DATE", severity=W, field="invoice_date", message="Invoice date missing."))
    elif date.fromisoformat(inv.invoice_date) > today:
        issues.append(Issue(code="FUTURE_DATED_INVOICE", severity=W, field="invoice_date",
                            message=f"Invoice is dated {inv.invoice_date}, in the future - billing before supply?"))
    if not inv.due_date:
        return issues
    due = date.fromisoformat(inv.due_date)
    if inv.invoice_date:
        issued = date.fromisoformat(inv.invoice_date)
        days = (due - issued).days
        if days < 0:
            issues.append(Issue(code="DUE_BEFORE_INVOICE", severity=E, field="due_date",
                                message=f"Due date {inv.due_date} is before invoice date {inv.invoice_date}."))
        terms_days = parse_terms_days(inv.payment_terms)
        if terms_days is not None and days >= 0 and abs(days - terms_days) > terms_tolerance_days:
            issues.append(Issue(code="TERMS_MISMATCH", severity=W, field="due_date",
                                message=f"Terms say '{inv.payment_terms}' but due date is {days} days after the invoice date."))
    if due < today:
        issues.append(Issue(code="PAST_DUE", severity=I, field="due_date", message=f"Past due by {(today - due).days} days - check for late fees."))
    return issues


def check_vendor(inv: Invoice, store: Store) -> list[Issue]:
    if not inv.vendor:
        return []
    row, match = store.find_vendor(inv.vendor)
    if match == "none":
        return [Issue(code="UNKNOWN_VENDOR", severity=W, field="vendor",
                      message=f"'{inv.vendor}' is not in the approved vendor master - verify before first payment.")]
    issues = []
    if match == "fuzzy":
        issues.append(Issue(code="VENDOR_NAME_VARIANT", severity=W, field="vendor",
                            message=f"'{inv.vendor}' closely resembles approved vendor '{row['name']}' - confirm it's the same entity."))
    elif match == "suffix":
        issues.append(Issue(code="VENDOR_NAME_VARIANT", severity=W, field="vendor",
                            message=f"'{inv.vendor}' has a different legal form from approved vendor '{row['name']}' - "
                                    "a different company with a similar name is a classic impersonation."))
    elif match == "alias":
        issues.append(Issue(code="VENDOR_ALIAS", severity=I, field="vendor", message=f"'{inv.vendor}' is a known alias of '{row['name']}'."))
    on_file = row.get("address")
    for label, address in (("Vendor address", inv.vendor_address), ("Remit-to address", inv.remit_to_address)):
        if address and on_file and normalize_name(address) != normalize_name(on_file):
            issues.append(Issue(code="VENDOR_ADDRESS_MISMATCH", severity=W, field="vendor_address",
                                message=f"{label} '{address}' differs from the address on file '{on_file}'."))
    if inv.remit_to_address and not on_file and inv.vendor_address and normalize_name(inv.remit_to_address) != normalize_name(inv.vendor_address):
        issues.append(Issue(code="VENDOR_ADDRESS_MISMATCH", severity=W, field="vendor_address",
                            message=f"Payment is to be sent to '{inv.remit_to_address}', not the vendor's own address '{inv.vendor_address}'."))
    return issues


def check_currency(inv: Invoice, policy: PolicySettings) -> list[Issue]:
    currency = (inv.currency or "USD").upper()
    if currency == policy.base_currency:
        return []
    if currency not in policy.fx_rates:
        return [Issue(code="UNSUPPORTED_CURRENCY", severity=E, field="currency", message=f"Currency {currency} is not supported.")]
    usd = to_usd(inv.total, currency, policy.fx_rates)
    return [Issue(code="FOREIGN_CURRENCY", severity=W, field="currency",
                  message=f"Invoice is in {currency} (~${usd:,.2f} USD at reference rate {policy.fx_rates[currency]}); "
                          "catalog prices can't be compared and treasury must confirm the FX rate.")]


def check_duplicates(inv: Invoice, store: Store) -> list[Issue]:
    issues: list[Issue] = []
    fp = fingerprint(inv)
    for prior in store.find_prior_invoices(inv.invoice_number, inv.vendor):
        same_vendor = normalize_name(prior["vendor"]) == normalize_name(inv.vendor)
        where = f"{Path(prior['source_file'] or 'unknown file').name} on {prior['processed_at'][:10]}"
        if prior["status"] in {"approved", "paid", "pending_review", "on_hold"}:
            if prior["fingerprint"] == fp:
                issues.append(Issue(code="DUPLICATE_INVOICE", severity=E, field="invoice_number",
                                    message=f"{inv.invoice_number} was already {prior['status'].replace('_', ' ')} ({where}) - duplicate."))
            elif same_vendor:
                issues.append(Issue(code="REVISED_INVOICE", severity=W, field="invoice_number",
                                    message=f"{inv.invoice_number} was already {prior['status'].replace('_', ' ')} for ${prior['total']:,.2f} ({where}); "
                                            f"this version is ${inv.total or 0:,.2f}. Pay at most the difference, after confirming the PO amendment."))
            else:
                issues.append(Issue(code="INVOICE_NUMBER_COLLISION", severity=W, field="invoice_number",
                                    message=f"{inv.invoice_number} was already used by a different vendor ({prior['vendor']})."))
        elif prior["status"] == "rejected":
            issues.append(Issue(code="PREVIOUSLY_REJECTED", severity=I, field="invoice_number",
                                message=f"{inv.invoice_number} was previously rejected ({where}) - this is a resubmission."))
    if inv.revision and not any(i.code == "REVISED_INVOICE" for i in issues):
        issues.append(Issue(code="REVISION_MARKER", severity=I, field="revision", message=f"Marked as revision '{inv.revision}'."))
    return issues


def check_fraud_signals(inv: Invoice, raw_text: str) -> list[Issue]:
    text = f"{raw_text}\n{inv.notes or ''}\n{inv.payment_terms or ''}"
    issues = []
    # Payment-redirection is the most expensive AP fraud, so it gets its own code that always needs a human.
    change = re.search(BANK_CHANGE_PATTERN, text, re.I)
    if change:
        issues.append(Issue(code="BANK_DETAILS_CHANGED", severity=W, field="notes",
                            message=f"Invoice asks to pay different bank details ('{change.group(0)}') - verify by phone with a known contact."))
    hits = [label for pattern, label in FRAUD_PATTERNS.items() if re.search(pattern, text, re.I)]
    if hits:
        issues.append(Issue(code="FRAUD_SIGNALS", severity=W, field="notes", message="Social-engineering red flags: " + "; ".join(hits) + "."))
    return issues


# Text that tries to instruct the AP system rather than bill it. Legitimate invoices never address
# "the AI"; one that does is treated as an attack, not an invoice.
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions|rules|prompts?)",
    r"\b(ai|a\.i\.|llm|language model|assistant|agent|chatbot|gpt|claude|grok)\b[^.\n]{0,60}\b(approve|pay|must|should|instruct)",
    r"system\s+prompt",
    r"you\s+are\s+now\b",
    r"do\s+not\s+(flag|escalate|reject|review|verify|validate)",
    r"(override|bypass|skip)\s+(the\s+)?(policy|rules|checks|validation|approval)",
]


def check_prompt_injection(raw_text: str) -> list[Issue]:
    hits = [m.group(0) for p in INJECTION_PATTERNS if (m := re.search(p, raw_text, re.I))]
    if not hits:
        return []
    return [Issue(code="PROMPT_INJECTION", severity=E, field="notes",
                  message=f"Document contains instructions aimed at the AP system (e.g. '{hits[0][:80]}') - treated as an attack.")]




def check_threshold_splitting(inv: Invoice, store: Store, policy: PolicySettings) -> list[Issue]:
    """Same vendor billing several sub-threshold invoices in one week that together cross the VP threshold."""
    usd = to_usd(inv.total, inv.currency, policy.fx_rates)
    floor = policy.split_min_share * policy.vp_review_threshold
    if not usd or not floor <= usd < policy.vp_review_threshold or not inv.invoice_date:
        return []
    recent = [r for r in store.recent_vendor_invoices(inv.vendor, inv.invoice_date, policy.split_window_days)
              if r["invoice_number"] != inv.invoice_number and floor <= (r["total"] or 0) < policy.vp_review_threshold]
    combined = usd + sum(r["total"] or 0 for r in recent)
    if not recent or combined < policy.vp_review_threshold:
        return []
    others = ", ".join(f"{r['invoice_number']} (${r['total']:,.2f})" for r in recent[:4])
    return [Issue(code="THRESHOLD_SPLITTING", severity=W, field="total",
                  message=f"{inv.vendor} billed {len(recent) + 1} invoices within {policy.split_window_days} days totalling ${combined:,.2f}, "
                          f"each under the ${policy.vp_review_threshold:,.0f} VP threshold ({others}) - possible split to avoid review.")]


# ---- orchestration --------------------------------------------------------------------------

CHECKS = ["required_fields", "line_items_and_stock", "arithmetic", "charges", "dates", "vendor", "currency", "duplicates", "fraud_signals",
          "prompt_injection", "threshold_splitting"]


def run_all_checks(inv: Invoice, raw_text: str, store: Store, policy: PolicySettings, today: date | None = None) -> ValidationReport:
    issues = [
        *check_required_fields(inv),
        *check_line_items(inv, store, policy),
        *check_arithmetic(inv),
        *check_charges(inv, policy),
        *check_dates(inv, today, policy.terms_tolerance_days),
        *check_vendor(inv, store),
        *check_currency(inv, policy),
        *check_duplicates(inv, store),
        *check_fraud_signals(inv, raw_text),
        *check_prompt_injection(raw_text),
        *check_threshold_splitting(inv, store, policy),
    ]
    rank = {E: 0, W: 1, I: 2}
    issues.sort(key=lambda i: rank[i.severity])  # stable: keeps check order within a severity
    report = ValidationReport(issues=issues, checks_run=list(CHECKS), total_usd=to_usd(inv.total, inv.currency, policy.fx_rates))
    report.risk_score = min(100, 35 * len(report.errors) + 12 * len(report.warnings))
    return report
