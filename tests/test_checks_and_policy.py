from datetime import date

import pytest

from invoice_agents.agents.ingestion import finalize
from invoice_agents.checks import run_all_checks
from invoice_agents.models import Invoice, LineItem
from invoice_agents.parsers import heuristic_extract, load_document
from invoice_agents.policy import assess

from .conftest import DATA

TODAY = date(2026, 1, 31)


def validate(name, store, settings):
    doc = load_document(DATA / name)
    inv = finalize(doc.structured or heuristic_extract(doc.text))
    report = run_all_checks(inv, doc.text, store, settings.policy, today=TODAY)
    return inv, report


def codes(report, severity=None):
    return {i.code for i in report.issues if severity is None or i.severity.value == severity}


# The scenario table from the case brief, plus the traps we found in the data.
@pytest.mark.parametrize("name,expected_error", [
    ("invoice_1002.txt", "INSUFFICIENT_STOCK"),     # 20x GadgetX, 5 in stock
    ("invoice_1003.txt", "OUT_OF_STOCK_ITEM"),      # FakeItem, 0 stock
    ("invoice_1003.txt", "INVALID_DUE_DATE"),       # 'yesterday'
    ("invoice_1008.txt", "UNKNOWN_ITEM"),           # SuperGizmo, MegaSprocket
    ("invoice_1016.json", "UNKNOWN_ITEM"),          # WidgetC
    ("invoice_1009.json", "INVALID_QUANTITY"),      # -5 WidgetA
    ("invoice_1009.json", "MISSING_VENDOR"),
    ("invoice_1007.csv", "TOTAL_MISMATCH"),         # 14750 + 885 != 15525
    ("invoice_1013.json", "TOTAL_MISMATCH"),        # 21040 + 1472.80 != 22562.80
    ("invoice_1013.json", "INSUFFICIENT_STOCK"),    # WidgetA split over 3 lines = 22 > 15
    ("invoice_1005.json", "INSUFFICIENT_STOCK"),    # GadgetX 8 > 5
])
def test_problem_invoices_are_blocked(name, expected_error, store, settings):
    _, report = validate(name, store, settings)
    assert expected_error in codes(report, "error")


@pytest.mark.parametrize("name", ["invoice_1001.txt", "invoice_1004.json", "invoice_1006.csv", "invoice_1011.pdf", "invoice_1015.csv"])
def test_clean_invoices_pass(name, store, settings):
    _, report = validate(name, store, settings)
    assert report.passed and not report.warnings, report.issues


def test_split_lines_are_aggregated_against_stock(store, settings):
    _, report = validate("invoice_1013.json", store, settings)
    msg = next(i.message for i in report.errors if i.code == "INSUFFICIENT_STOCK" and "WidgetA" in i.message)
    assert "22 units across 3 lines" in msg


def test_rush_premium_is_a_warning_not_error(store, settings):
    inv, report = validate("invoice_1010.txt", store, settings)
    assert report.passed
    assert codes(report, "warning") == {"PRICE_ABOVE_CATALOG"}
    assert all(li.sku for li in inv.line_items)  # 'WidgetA (rush order)' still maps to WidgetA


def test_ocr_vendor_typo_and_spaced_skus(store, settings):
    inv, report = validate("invoice_1012.txt", store, settings)
    assert [li.sku for li in inv.line_items] == ["WidgetA", "WidgetB", "GadgetX"]
    assert "VENDOR_NAME_VARIANT" in codes(report, "warning")


def test_terms_mismatch(store, settings):
    _, report = validate("invoice_1002.txt", store, settings)
    assert "TERMS_MISMATCH" in codes(report, "warning")


def test_fraud_signals(store, settings):
    _, report = validate("invoice_1003.txt", store, settings)
    assert "FRAUD_SIGNALS" in codes(report) and "UNKNOWN_VENDOR" in codes(report)


def test_duplicate_and_revision(store, settings):
    inv, _ = validate("invoice_1004.json", store, settings)
    store.record_invoice(run_id="r1", invoice=inv.model_dump(), status="paid", decision="approve", reasoning="",
                         fingerprint=__import__("invoice_agents.checks", fromlist=["fingerprint"]).fingerprint(inv),
                         source_file="invoice_1004.json", result={})
    _, again = validate("invoice_1004.json", store, settings)
    assert "DUPLICATE_INVOICE" in codes(again, "error")
    _, revised = validate("invoice_1004_revised.json", store, settings)
    assert "REVISED_INVOICE" in codes(revised, "warning") and revised.passed


def test_vendor_address_change_is_flagged(store, settings):
    inv = Invoice(invoice_number="INV-9", vendor="Widgets Inc.", vendor_address="1 Offshore Way, Cayman",
                  due_date="2026-02-20", invoice_date="2026-01-20", total=250.0,
                  line_items=[LineItem(item="WidgetA", quantity=1, unit_price=250.0)])
    report = run_all_checks(inv, "", store, settings.policy, today=TODAY)
    assert "VENDOR_ADDRESS_MISMATCH" in codes(report, "warning")


# ---- policy -----------------------------------------------------------------------------------


def test_policy_errors_force_reject(store, settings):
    inv, report = validate("invoice_1002.txt", store, settings)
    assert assess(inv, report, settings.policy).allowed == ["reject"]


def test_policy_foreign_currency_escalates(store, settings):
    inv, report = validate("invoice_1014.xml", store, settings)
    a = assess(inv, report, settings.policy)
    assert a.recommended == "escalate" and "approve" not in a.allowed


def test_policy_clean_small_invoice_approves(store, settings):
    inv, report = validate("invoice_1001.txt", store, settings)
    a = assess(inv, report, settings.policy)
    assert (a.tier, a.recommended) == ("standard", "approve")


def test_policy_vp_tier_and_cfo_tier(store, settings):
    def big(total):
        qty = total / 250
        return Invoice(invoice_number="INV-1", vendor="Widgets Inc.", invoice_date="2026-01-01", due_date="2026-01-31",
                       payment_terms="Net 30", total=total, line_items=[LineItem(item="WidgetA", quantity=qty, unit_price=250)])
    from dataclasses import replace

    roomy = replace(settings.policy)
    with store.connect() as conn:
        conn.execute("UPDATE inventory SET stock = 1000 WHERE item = 'WidgetA'")
    for total, tier, can_approve in [(12_500, "vp", True), (60_000, "cfo", False)]:
        inv = big(total)
        report = run_all_checks(inv, "", store, roomy, today=TODAY)
        a = assess(inv, report, roomy)
        assert a.tier == tier and ("approve" in a.allowed) == can_approve
