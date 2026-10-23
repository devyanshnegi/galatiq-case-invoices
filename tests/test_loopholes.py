"""Regression tests for the loopholes found in the full code review. One test per finding."""

import contextlib
import io
import json
import sqlite3
from dataclasses import replace

import pytest

from invoice_agents import bank, review
from invoice_agents.agents.ingestion import extraction_problems
from invoice_agents.agents.payment import settle_unconfirmed
from invoice_agents.backends import LocalBackend
from invoice_agents.checks import run_all_checks
from invoice_agents.config import load_settings
from invoice_agents.graph import InvoicePipeline
from invoice_agents.models import Invoice, LineItem
from invoice_agents.normalize import normalize_invoice_number
from invoice_agents.parsers import load_document
from invoice_agents.policy import policy_problems

from .conftest import DATA


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def base(**over):
    data = {"invoice_number": "INV-8800", "vendor": {"name": "Widgets Inc."}, "date": "2026-10-01", "due_date": "2026-10-31",
            "payment_terms": "Net 30", "line_items": [{"item": "WidgetA", "quantity": 4, "unit_price": 250.0}],
            "subtotal": 1000.0, "total": 1000.0}
    return {**data, **over}


@pytest.fixture
def pipeline(settings):
    with quiet(), InvoicePipeline(settings) as p:
        yield p


def process(pipeline, path):
    with quiet():
        return pipeline.process(path)


def codes(state):
    return {i["code"] for i in state["validation"]["issues"]}


# ---- money -----------------------------------------------------------------------------------------


def test_bank_limits_are_in_usd(store):
    with quiet():
        out = bank.pay(store, invoice_id=1, invoice_number="X", vendor="V", amount=49_000, currency="EUR",
                       single_payment_limit=50_000, fx_rates={"USD": 1.0, "EUR": 1.08})
    assert out["status"] == "declined" and "$52,920.00" in out["reason"]  # 49,000 EUR is over a $50K limit


def test_bank_refuses_currencies_it_has_no_rate_for(store):
    out = bank.pay(store, invoice_id=1, invoice_number="X", vendor="V", amount=10, currency="JPY",
                   single_payment_limit=50_000, fx_rates={"USD": 1.0})
    assert out["status"] == "declined"


def test_unknown_payment_outcome_is_never_retried_into_a_double_payment(store, monkeypatch):
    def crash(vendor, amount):
        raise TimeoutError("connection dropped after sending")

    monkeypatch.setattr(bank, "mock_payment", crash)
    first = bank.pay(store, invoice_id=7, invoice_number="X", vendor="V", amount=10, currency="USD", single_payment_limit=1e6)
    monkeypatch.setattr(bank, "mock_payment", lambda v, a: {"status": "success"})
    retry = bank.pay(store, invoice_id=7, invoice_number="X", vendor="V", amount=10, currency="USD", single_payment_limit=1e6)
    assert first["status"] == retry["status"] == "unknown"  # the retry did NOT pay
    assert store.payment_for_invoice(7)["status"] == "unknown"


def test_unclear_bank_answer_counts_as_unknown(store, monkeypatch):
    monkeypatch.setattr(bank, "mock_payment", lambda v, a: {"status": "pending"})
    assert bank.pay(store, invoice_id=8, invoice_number="X", vendor="V", amount=10, currency="USD", single_payment_limit=1e6)["status"] == "unknown"


def test_definite_decline_frees_the_slot_for_a_retry(store, monkeypatch):
    monkeypatch.setattr(bank, "mock_payment", lambda v, a: {"status": "declined"})
    assert bank.pay(store, invoice_id=9, invoice_number="X", vendor="V", amount=10, currency="USD", single_payment_limit=1e6)["status"] == "declined"
    monkeypatch.setattr(bank, "mock_payment", lambda v, a: {"status": "success"})
    assert bank.pay(store, invoice_id=9, invoice_number="X", vendor="V", amount=10, currency="USD", single_payment_limit=1e6)["status"] == "success"


def test_pipeline_flags_unknown_outcome_instead_of_marking_paid(pipeline, monkeypatch):
    monkeypatch.setattr(bank, "mock_payment", lambda v, a: (_ for _ in ()).throw(ConnectionError("reset")))
    state = process(pipeline, DATA / "invoice_1001.txt")
    row = pipeline.store.get_invoice(state["invoice_id"])
    assert state["status"] == "failed" and "OUTCOME UNKNOWN" in row["reasoning"] and "reconcile" in row["reasoning"]


def test_revision_pays_only_the_difference(pipeline):
    process(pipeline, DATA / "invoice_1004.json")  # $1,890 paid
    iid = process(pipeline, DATA / "invoice_1004_revised.json")["invoice_id"]  # $5,940, escalated
    with quiet():
        out = review.approve(pipeline.store, iid, reviewer="Dana Lee", role="vp_finance", note="PO amended",
                             backend=pipeline.backend, settings=pipeline.settings)
    assert out["status"] == "paid" and out["payment"]["amount"] == 4050.0
    assert sum(p["amount"] for p in pipeline.store.list_payments() if p["invoice_number"] == "INV-1004") == 5940.0


def test_revision_already_covered_pays_nothing(pipeline, tmp_path):
    process(pipeline, DATA / "invoice_1004.json")  # $1,890 paid
    lower = write(tmp_path, "rev.json", json.loads((DATA / "invoice_1004.json").read_text()) | {"total": 1500.0, "subtotal": 1360.0,
                                                                                                 "tax_amount": 140.0, "line_items": [
        {"item": "WidgetA", "quantity": 3, "unit_price": 250.0}, {"item": "WidgetB", "quantity": 1, "unit_price": 610.0}]})
    iid = process(pipeline, lower)["invoice_id"]
    with quiet():
        out = review.approve(pipeline.store, iid, reviewer="Dana Lee", role="vp_finance", note="credit agreed",
                             backend=pipeline.backend, settings=pipeline.settings)
    assert out["status"] == "paid" and "nothing further due" in out["message"]
    assert sum(1 for p in pipeline.store.list_payments() if p["invoice_number"] == "INV-1004") == 1


def test_settle_unconfirmed_finishes_crashed_batch_payments(pipeline):
    state = process(pipeline, DATA / "invoice_1001.txt")
    iid = state["invoice_id"]
    with pipeline.store.connect() as conn:  # simulate: crashed after 'approved', before the bank call
        conn.execute("DELETE FROM payments")
        conn.execute("UPDATE invoices SET status = 'approved' WHERE id = ?", (iid,))
    with quiet():
        report = settle_unconfirmed(pipeline.store, pipeline.backend, older_than_s=0)
    assert report[0]["outcome"] == "paid" and pipeline.store.get_invoice(iid)["status"] == "paid"


# ---- human review ------------------------------------------------------------------------------------


def test_only_roster_members_may_act_and_only_in_their_role(pipeline):
    iid = process(pipeline, DATA / "invoice_1014.xml")["invoice_id"]
    for who, role, msg in [("Mallory", "cfo", "not on the approver roster"), ("Ann Kim", "cfo", "registered as AP specialist")]:
        with pytest.raises(review.ReviewError, match=msg):
            review.approve(pipeline.store, iid, reviewer=who, role=role, backend=pipeline.backend, settings=pipeline.settings)
        with pytest.raises(review.ReviewError, match=msg):
            review.hold(pipeline.store, iid, reviewer=who, role=role, note="x", settings=pipeline.settings)


def test_one_person_never_counts_twice(pipeline, tmp_path):
    with pipeline.store.connect() as conn:
        conn.execute("UPDATE inventory SET stock = 100000 WHERE item = 'WidgetA'")
    iid = process(pipeline, write(tmp_path, "big.json", base(invoice_number="INV-8801", line_items=[{"item": "WidgetA", "quantity": 240,
                                                                                                    "unit_price": 250.0}],
                                                       subtotal=60000.0, total=60000.0)))["invoice_id"]
    for _ in range(2):  # e.g. two simultaneous clicks recorded by two processes
        pipeline.store.add_approval(invoice_id=iid, reviewer="Sam Patel", role="cfo", action="approve")
    case = review.load_case(pipeline.store, iid, pipeline.settings.policy)
    assert len(case.approvers) == 1 and case.missing()


def test_rejecting_needs_a_reason(pipeline):
    iid = process(pipeline, DATA / "invoice_1014.xml")["invoice_id"]
    with pytest.raises(review.ReviewError, match="reason"):
        review.reject(pipeline.store, iid, reviewer="Dana Lee", role="vp_finance", note=" ", settings=pipeline.settings)


def test_mcp_cannot_choose_who_it_is(settings):
    import asyncio

    from mcp import Client

    from invoice_agents.mcp_servers import ap

    async def tools_and_call(server):
        async with Client(server) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            result = await c.call_tool("resolve_invoice", {"invoice_id": 1, "approve": True})
            return tools, json.loads("".join(x.text for x in result.content))

    with quiet(), InvoicePipeline(settings, quiet_payments=True) as p:
        tools, result = asyncio.run(tools_and_call(ap.build_server(p, reviewer="")))
    assert "reviewer" not in tools["resolve_invoice"].input_schema["properties"]
    assert "disabled" in result["error"]


# ---- validation ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("over,code,status", [
    ({"line_items": [{"item": "WidgetA", "quantity": 4}], "subtotal": None, "total": 9999.0}, "MISSING_PRICE", "rejected"),
    ({"line_items": [{"item": "WidgetA", "quantity": 4, "amount": 1000.0}], "subtotal": None, "total": 9999.0}, "TOTAL_MISMATCH", "rejected"),
    ({"tax_amount": 400.0, "total": 1400.0}, "TAX_RATE_UNUSUAL", "pending_review"),
    ({"shipping": 400.0, "total": 1400.0}, "FREIGHT_ABOVE_TOLERANCE", "pending_review"),
    ({"vendor": {"name": "Widgets LLC"}}, "VENDOR_NAME_VARIANT", "pending_review"),
    ({"vendor": {"name": "Widgets Inc.", "address": "100 Main St, Chicago, IL 60601",
                 "remit_to": {"address": "PO Box 9, Nassau, Bahamas"}}}, "VENDOR_ADDRESS_MISMATCH", "pending_review"),
    ({"invoice_number": None}, "MISSING_INVOICE_NUMBER", "pending_review"),
    ({"date": "2027-06-01", "due_date": "2027-07-01"}, "FUTURE_DATED_INVOICE", "pending_review"),
])
def test_inflated_or_suspicious_invoices_are_not_auto_paid(pipeline, tmp_path, over, code, status):
    state = process(pipeline, write(tmp_path, "inv.json", {k: v for k, v in base(**over).items() if v is not None}))
    assert code in codes(state) and state["status"] == status


def test_normal_invoices_are_unaffected(pipeline, tmp_path):
    state = process(pipeline, write(tmp_path, "ok.json", base(tax_amount=82.5, tax_rate=0.0825, shipping=40.0, total=1122.5)))
    assert state["status"] == "paid"


def test_invoice_numbers_keep_what_makes_them_different():
    assert normalize_invoice_number("2026-0001") != normalize_invoice_number("2026-0002")
    assert normalize_invoice_number("INV-1001-A") != normalize_invoice_number("INV-1001-B")
    assert normalize_invoice_number("inv 1012") == normalize_invoice_number("INV-1012") == "INV-1012"


def test_template_remit_to_is_parsed():
    inv = load_document(DATA.parent / "templates" / "invoice_template.json").structured
    assert inv.remit_to_address == "100 Main St, Chicago, IL 60601"


def test_extraction_self_check_catches_a_misread_currency():
    inv = Invoice(vendor="TechParts", total=4125.0, currency="USD", due_date_raw="2026-02-26",
                  line_items=[LineItem(item="WidgetA", quantity=4, unit_price=225)])
    assert any("currency" in p for p in extraction_problems(inv, "Vendor: TechParts\nWidgetA 4 225.00 EUR\nTotal: 4125.00 EUR"))


# ---- operations and configuration ---------------------------------------------------------------------


def test_reset_refuses_without_confirmation_when_not_interactive(tmp_path, monkeypatch):
    from cli import app

    monkeypatch.setenv("INVENTORY_DB", str(tmp_path / "r.db"))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # not a terminal
    with quiet():
        assert app.main(["reset"]) == 1
        assert app.main(["reset", "--yes"]) == 0


def test_policy_validator_checks_the_roster():
    s = load_settings(provider="offline")
    broken = replace(s, policy=replace(s.policy, reviewers={"dana lee": ("Dana Lee", "vp_finance")}))
    assert any("CFO" in p or "cfo" in p for p in policy_problems(broken))
    assert policy_problems(s) == []


def test_database_duplicate_guard_records_an_honest_reason(pipeline, monkeypatch):
    import invoice_agents.agents.payment as payment_mod

    process(pipeline, DATA / "invoice_1001.txt")
    monkeypatch.setattr(payment_mod, "check_duplicates", lambda inv, store: [])  # simulate another process's race
    monkeypatch.setattr("invoice_agents.checks.check_duplicates", lambda inv, store: [])
    state = process(pipeline, DATA / "invoice_1001.txt")
    row = pipeline.store.get_invoice(state["invoice_id"])
    stored = json.loads(row["result_json"])
    assert state["status"] == "rejected" and stored["decision"]["decision"] == "reject"
    assert "DUPLICATE_INVOICE" in {i["code"] for i in stored["validation"]["issues"]}
    with pytest.raises(sqlite3.IntegrityError):  # and the database itself refuses a second payable copy
        with pipeline.store.connect() as conn:
            conn.execute("UPDATE invoices SET status = 'pending_review' WHERE id = ?", (state["invoice_id"],))
            conn.execute("UPDATE invoices SET fingerprint = (SELECT fingerprint FROM invoices WHERE status = 'paid' LIMIT 1) WHERE id = ?",
                         (state["invoice_id"],))


@pytest.mark.parametrize("over", [{"tax_amount": 400.0, "total": 1400.0}, {"shipping": 400.0, "total": 1400.0}])
def test_inflated_tax_or_freight_always_goes_to_a_human_even_if_the_model_says_approve(settings, tmp_path, over):
    """The policy forbids approving these, so a model that tries is overridden."""
    from .conftest import FakeLLM

    yes = {"decision": "approve", "reasoning": "Looks fine.", "key_factors": [], "conditions": ["none"]}
    llm = FakeLLM({"RiskAssessment": [{"suspicious": False, "signals": []}], "ApprovalDecision": [yes],
                   "Critique": [{"agrees": True, "problems": [], "recommended_decision": "approve"}]})
    with quiet(), InvoicePipeline(settings, llm=llm) as p:
        state = p.process(write(tmp_path, "inv.json", base(**over)))
    assert state["policy"]["allowed"] == ["escalate", "reject"]
    assert state["status"] == "pending_review"
