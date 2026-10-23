"""The complete invoice template is valid end to end, and line discounts are validated."""

import contextlib
import io
import json
from pathlib import Path

from invoice_agents.checks import check_arithmetic
from invoice_agents.graph import InvoicePipeline
from invoice_agents.models import Invoice, LineItem
from invoice_agents.parsers import load_document

TEMPLATE = Path(__file__).resolve().parent.parent / "data" / "templates" / "invoice_template.json"


def test_template_is_internally_consistent():
    data = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    lines = data["line_items"]
    for li in lines:
        assert round(li["quantity"] * li["unit_price"] - li["discount_amount"], 2) == li["amount"]
        assert round(li["amount"] * li["tax_rate"], 2) == li["tax_amount"]
    assert round(sum(li["amount"] for li in lines), 2) == data["subtotal"]
    assert round(sum(li["discount_amount"] for li in lines), 2) == data["total_discount"]
    assert round(sum(t["amount"] for t in data["tax_summary"]), 2) == data["tax_amount"]
    assert round(data["subtotal"] + data["tax_amount"] + data["total_charges"], 2) == data["total"]
    assert round(data["total"] - data["amount_paid"] - data["credits_applied"], 2) == data["balance_due"]


def test_template_parses_with_discounts():
    inv = load_document(TEMPLATE).structured
    assert inv.vendor == "Widgets Inc." and inv.total == 6536.75
    assert [li.discount for li in inv.line_items] == [None, 100.0, None]


def test_template_passes_every_check_and_is_paid(settings):
    with contextlib.redirect_stdout(io.StringIO()), InvoicePipeline(settings) as pipeline:
        state = pipeline.process(TEMPLATE)
    blocking = [i for i in state["validation"]["issues"] if i["severity"] != "info"]
    assert blocking == [] and state["status"] == "paid"


def test_wrong_discounted_line_total_is_caught():
    inv = Invoice(line_items=[LineItem(item="WidgetB", quantity=4, unit_price=500, discount=100, amount=2000)],
                  subtotal=2000, total=2000)
    codes = {i.code for i in check_arithmetic(inv)}
    assert "LINE_TOTAL_MISMATCH" in codes


def test_discount_larger_than_line_is_invalid():
    inv = Invoice(line_items=[LineItem(item="WidgetB", quantity=1, unit_price=500, discount=600, amount=-100)], total=1)
    assert "INVALID_DISCOUNT" in {i.code for i in check_arithmetic(inv)}


def test_discount_percent_is_converted_to_amount(tmp_path):
    path = tmp_path / "pct.json"
    path.write_text(json.dumps({"vendor": "Widgets Inc.", "total": 1900,
                                "line_items": [{"item": "WidgetB", "quantity": 4, "unit_price": 500, "discount_percent": 0.05, "amount": 1900}]}))
    assert load_document(path).structured.line_items[0].discount == 100.0
