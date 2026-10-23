import pytest

from invoice_agents.normalize import fix_ocr_digits, normalize_invoice_number, parse_date, parse_money, parse_terms_days
from invoice_agents.parsers import heuristic_extract, load_document

from .conftest import DATA


@pytest.mark.parametrize("raw,expected", [
    ("2026-01-15", "2026-01-15"),
    ("Jan 30 2026", "2026-01-30"),
    ("January 27, 2026", "2026-01-27"),
    ("26-Jan-2O26", "2026-01-26"),  # OCR letter O
    ("01/28/2026", "2026-01-28"),
    ("yesterday", None),
    ("Net 30", None),
    (None, None),
])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("$3,500.O0", 3500.0), ("$15,000.00", 15000.0), ("(250.00)", -250.0), ("", None), (None, None), (12, 12.0),
])
def test_parse_money(raw, expected):
    assert parse_money(raw) == expected


def test_ocr_fix_leaves_words_alone():
    assert fix_ocr_digits("QuickShip 2O26 Ltd. Order") == "QuickShip 2026 Ltd. Order"


@pytest.mark.parametrize("raw,expected", [("1002", "INV-1002"), ("INV 1012", "INV-1012"), ("INV-1001", "INV-1001"), (None, None)])
def test_invoice_number(raw, expected):
    assert normalize_invoice_number(raw) == expected


def test_terms():
    assert parse_terms_days("Net 30") == 30
    assert parse_terms_days("Immediate") == 0
    assert parse_terms_days("") is None


@pytest.mark.parametrize("name", sorted(p.name for p in DATA.iterdir() if p.suffix != ".py"))
def test_every_sample_loads_and_yields_core_fields(name):
    doc = load_document(DATA / name)
    inv = doc.structured or heuristic_extract(doc.text)
    assert inv.line_items, "no line items"
    assert inv.total is not None
    if name != "invoice_1009.json":  # intentionally has no vendor
        assert inv.vendor


def test_kv_csv_layout():
    inv = load_document(DATA / "invoice_1006.csv").structured
    assert [(li.item, li.quantity) for li in inv.line_items] == [("WidgetA", 5), ("WidgetB", 3)]
    assert inv.total == 2750.0


def test_tabular_csv_summary_rows():
    inv = load_document(DATA / "invoice_1007.csv").structured
    assert (inv.subtotal, inv.tax, inv.tax_rate, inv.total) == (14750.0, 885.0, 0.06, 15525.0)


def test_xml_currency():
    assert load_document(DATA / "invoice_1014.xml").structured.currency == "EUR"


def test_heuristic_handles_typos_and_email():
    inv = heuristic_extract(load_document(DATA / "invoice_1002.txt").text)
    assert (inv.vendor, inv.invoice_date, inv.total) == ("Gadgets Co.", "2026-01-30", 15000.0)
    email = heuristic_extract(load_document(DATA / "invoice_1008.txt").text)
    assert email.vendor == "NoProd Industries"  # not the From: email address
    assert [li.item for li in email.line_items] == ["SuperGizmo", "MegaSprocket"]


def test_heuristic_keeps_relative_due_date_raw():
    inv = heuristic_extract(load_document(DATA / "invoice_1003.txt").text)
    assert inv.due_date is None and inv.due_date_raw == "yesterday"
    assert "Wire transfer" in inv.notes


def test_pdf_with_ocr_noise():
    inv = heuristic_extract(load_document(DATA / "invoice_1012.pdf").text)
    assert [li.item for li in inv.line_items] == ["Widget A", "WidgetB", "Gadget X"]
    assert inv.line_items[1].amount == 3500.0
    assert inv.invoice_date == "2026-01-26"


def test_unsupported_file(tmp_path):
    bad = tmp_path / "x.docx"
    bad.write_text("hi")
    with pytest.raises(ValueError):
        load_document(bad)


def test_negative_amount_with_sign_after_currency():
    assert parse_money("$-1,575.00") == -1575.0 and parse_money("-$20") == -20.0


def test_vendor_named_acme_something_is_not_the_customer():
    from invoice_agents.agents.ingestion import extraction_problems
    from invoice_agents.models import Invoice, LineItem

    text = "Vendor: Acme Industrial Supplies\nWidgetA qty: 2 unit price: $250.00\nTotal: $500.00"
    inv = Invoice(vendor="Acme Industrial Supplies", total=500.0, due_date_raw="x", line_items=[LineItem(item="WidgetA", quantity=2, unit_price=250)])
    assert not any("customer" in p for p in extraction_problems(inv, text))
    assert any("customer" in p for p in extraction_problems(inv.model_copy(update={"vendor": "Acme Corp"}), text))
