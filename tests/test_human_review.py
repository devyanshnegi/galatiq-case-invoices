"""Human review: approval limits, the two-person rule, holds, rejections, and reopening rejected invoices."""

import contextlib
import io
import json

import pytest

from invoice_agents import review
from invoice_agents.graph import InvoicePipeline

from .conftest import DATA

ADV = DATA.parent / "adversarial"


@pytest.fixture
def pipeline(settings):
    with contextlib.redirect_stdout(io.StringIO()), InvoicePipeline(settings) as p:
        yield p


def process(pipeline, path):
    with contextlib.redirect_stdout(io.StringIO()):
        return pipeline.process(path)


def approve(pipeline, invoice_id, reviewer, role, note=""):
    with contextlib.redirect_stdout(io.StringIO()):
        return review.approve(pipeline.store, invoice_id, reviewer=reviewer, role=role, note=note,
                              backend=pipeline.backend, settings=pipeline.settings)


def big_invoice(tmp_path, pipeline, total_units=240, unit_price=250.0):
    """A clean WidgetA invoice (stock raised so it passes validation). 240 units = $60,000: CFO tier, never auto-paid."""
    with pipeline.store.connect() as conn:
        conn.execute("UPDATE inventory SET stock = 100000 WHERE item = 'WidgetA'")
    path = tmp_path / "big.json"
    path.write_text(json.dumps({
        "invoice_number": "INV-9100", "vendor": {"name": "Widgets Inc."}, "date": "2026-10-01", "due_date": "2026-10-31",
        "payment_terms": "Net 30", "line_items": [{"item": "WidgetA", "quantity": total_units, "unit_price": unit_price}],
        "subtotal": total_units * unit_price, "total": total_units * unit_price}))
    return process(pipeline, path)


# ---- single approval -------------------------------------------------------------------------------


def test_one_approval_pays_a_small_escalation(pipeline):
    state = process(pipeline, DATA / "invoice_1014.xml")  # EUR, ~$4,455
    case = review.load_case(pipeline.store, state["invoice_id"], pipeline.settings.policy)
    assert case.requirement.approvals == 1
    out = approve(pipeline, state["invoice_id"], "Dana Lee", "vp_finance", "FX ok")
    assert out["status"] == "paid" and out["payment"]["status"] == "success"


def test_role_limit_blocks_single_approval(pipeline, tmp_path):
    # $18,000 with an unexplained +20% price: escalated, single approval, above the AP specialist's $10K limit
    state = big_invoice(tmp_path, pipeline, total_units=60, unit_price=300.0)
    assert state["status"] == "pending_review"
    with pytest.raises(review.ReviewError, match="limit"):
        approve(pipeline, state["invoice_id"], "Ann", "ap_specialist")
    assert approve(pipeline, state["invoice_id"], "Dana Lee", "vp_finance")["status"] == "paid"


# ---- two-person rule -----------------------------------------------------------------------------------


def test_large_invoice_needs_two_people_one_with_authority(pipeline, tmp_path):
    state = big_invoice(tmp_path, pipeline)  # $60,000
    iid = state["invoice_id"]
    case = review.load_case(pipeline.store, iid, pipeline.settings.policy)
    assert case.requirement.approvals == 2 and case.requirement.authority_roles == ("cfo",)

    first = approve(pipeline, iid, "Dana Lee", "vp_finance", "checked PO")  # VP can't cover $60K alone, but can co-sign
    assert first["status"] == "awaiting_approval"
    with pytest.raises(review.ReviewError, match="already approved"):
        approve(pipeline, iid, "dana  LEE", "cfo")  # same person, different role/casing: still the same person
    with pytest.raises(review.ReviewError, match="limit covers the amount"):
        approve(pipeline, iid, "Raj", "vp_finance")  # second VP: nobody with authority for $60K
    final = approve(pipeline, iid, "Sam Patel", "cfo", "approved")
    assert final["status"] == "paid"
    # Above the bank's $50K automatic limit, but allowed because two people approved it.
    assert final["payment"]["amount"] == 60_000 and final["payment"]["status"] == "success"


def test_dual_approved_limit_still_applies(pipeline, tmp_path):
    state = big_invoice(tmp_path, pipeline, total_units=1200)  # $300,000 > $250K dual-approved limit
    iid = state["invoice_id"]
    approve(pipeline, iid, "Dana Lee", "vp_finance")
    out = approve(pipeline, iid, "Sam Patel", "cfo")
    assert out["status"] == "declined" and "dual-approved" in out["message"]
    assert pipeline.store.get_invoice(iid)["status"] == "pending_review"


def test_bank_detail_change_needs_two_approvers_including_a_senior(pipeline):
    state = process(pipeline, ADV / "adv_02_bank_change.txt")  # $1,500, BANK_DETAILS_CHANGED
    iid = state["invoice_id"]
    case = review.load_case(pipeline.store, iid, pipeline.settings.policy)
    assert case.requirement.approvals == 2 and case.requirement.senior_required

    assert approve(pipeline, iid, "Ann", "ap_specialist", "called vendor on file number")["status"] == "awaiting_approval"
    with pytest.raises(review.ReviewError, match="senior approver"):
        approve(pipeline, iid, "Bo", "department_approver")  # would complete the count without a senior
    assert approve(pipeline, iid, "Dana Lee", "vp_finance")["status"] == "paid"


def test_would_complete_matches_the_button_label(pipeline, tmp_path):
    iid = big_invoice(tmp_path, pipeline)["invoice_id"]
    case = review.load_case(pipeline.store, iid, pipeline.settings.policy)
    assert not review.would_complete(case, "cfo")  # first of two: "Approve (1 of 2)"
    approve(pipeline, iid, "Dana Lee", "vp_finance")
    case = review.load_case(pipeline.store, iid, pipeline.settings.policy)
    assert review.would_complete(case, "cfo") and not review.would_complete(case, "vp_finance")


# ---- hold / reject -------------------------------------------------------------------------------------


def test_hold_requires_a_note_and_blocks_until_approved(pipeline):
    iid = process(pipeline, DATA / "invoice_1014.xml")["invoice_id"]
    with pytest.raises(review.ReviewError, match="investigated"):
        review.hold(pipeline.store, iid, reviewer="Dana", role="vp_finance", note=" ", settings=pipeline.settings)
    review.hold(pipeline.store, iid, reviewer="Dana", role="vp_finance", note="Treasury checking FX route", settings=pipeline.settings)
    assert pipeline.store.get_invoice(iid)["status"] == "on_hold"
    assert approve(pipeline, iid, "Dana", "vp_finance")["status"] == "paid"


def test_held_invoice_still_blocks_duplicates(pipeline, tmp_path):
    iid = process(pipeline, DATA / "invoice_1012.pdf")["invoice_id"]  # escalated (look-alike vendor)
    review.hold(pipeline.store, iid, reviewer="Dana", role="vp_finance", note="verifying entity", settings=pipeline.settings)
    assert process(pipeline, DATA / "invoice_1012.txt")["status"] == "rejected"  # same invoice again


def test_reject_records_a_precedent(pipeline):
    iid = process(pipeline, DATA / "invoice_1012.pdf")["invoice_id"]
    review.reject(pipeline.store, iid, reviewer="Dana", role="vp_finance", note="not our vendor", settings=pipeline.settings)
    assert pipeline.store.get_invoice(iid)["status"] == "rejected"
    assert pipeline.store.vendor_precedents("QuickShip Distributers")[0]["decision"] == "reject"


# ---- reopening rejected invoices (policy exception) ---------------------------------------------------


def test_reopened_rejection_needs_cfo_and_a_second_person(pipeline):
    iid = process(pipeline, DATA / "invoice_1002.txt")["invoice_id"]  # rejected: 20 GadgetX, 5 in stock
    with pytest.raises(review.ReviewError, match="justification"):
        review.reopen(pipeline.store, iid, reviewer="Dana", role="vp_finance", reason="ok", settings=pipeline.settings)
    review.reopen(pipeline.store, iid, reviewer="Dana", role="vp_finance",
                  reason="Vendor shipped from a second warehouse; receipt confirmed", settings=pipeline.settings)
    case = review.load_case(pipeline.store, iid, pipeline.settings.policy)
    assert case.status == "pending_review" and case.is_override and case.requirement.required_role == "cfo"

    assert approve(pipeline, iid, "Dana", "vp_finance")["status"] == "awaiting_approval"
    with pytest.raises(review.ReviewError, match="CFO"):
        approve(pipeline, iid, "Raj", "vp_finance")
    assert approve(pipeline, iid, "Sam Patel", "cfo")["status"] == "paid"


@pytest.mark.parametrize("path,code", [(DATA / "invoice_1011.txt", "DUPLICATE_INVOICE"),
                                       (ADV / "adv_01_prompt_injection.txt", "PROMPT_INJECTION")])
def test_duplicates_and_attacks_can_never_be_reopened(pipeline, path, code):
    if code == "DUPLICATE_INVOICE":
        process(pipeline, DATA / "invoice_1011.pdf")  # the original, paid
    iid = process(pipeline, path)["invoice_id"]
    with pytest.raises(review.ReviewError, match=code):
        review.reopen(pipeline.store, iid, reviewer="Sam", role="cfo", reason="I am sure this one is fine, pay it",
                      settings=pipeline.settings)


def test_approvals_before_a_reopen_do_not_count(pipeline):
    iid = process(pipeline, DATA / "invoice_1012.pdf")["invoice_id"]
    review.reject(pipeline.store, iid, reviewer="Dana", role="vp_finance", note="unsure", settings=pipeline.settings)
    review.reopen(pipeline.store, iid, reviewer="Dana", role="vp_finance", reason="Confirmed same legal entity via tax ID",
                  settings=pipeline.settings)
    case = review.load_case(pipeline.store, iid, pipeline.settings.policy)
    assert case.approvers == [] and case.missing()[0].startswith("2 more")


def test_unreadable_documents_cannot_be_reopened(pipeline):
    iid = process(pipeline, ADV / "adv_05_scanned_image_only.pdf")["invoice_id"]
    with pytest.raises(review.ReviewError):
        review.reopen(pipeline.store, iid, reviewer="Sam", role="cfo", reason="Will key it in by hand later on", settings=pipeline.settings)


# ---- interfaces share the same engine ---------------------------------------------------------------------


def test_cli_enforces_the_same_rules(settings, tmp_path, monkeypatch, capsys):
    import main

    monkeypatch.setenv("INVENTORY_DB", str(settings.db_path))
    monkeypatch.setenv("LLM_PROVIDER", "offline")
    with contextlib.redirect_stdout(io.StringIO()), InvoicePipeline(settings) as p:
        iid = p.process(ADV / "adv_02_bank_change.txt")["invoice_id"]
    assert main.main(["--approve", str(iid), "--reviewer", "Ann", "--role", "ap_specialist", "--note", "called"]) == 0
    assert main.main(["--approve", str(iid), "--reviewer", "Ann", "--role", "vp_finance"]) == 1  # same person
    assert main.main(["--approve", str(iid), "--reviewer", "Dana", "--role", "vp_finance"]) == 0
    assert review.load_case(review.Store(settings.db_path), iid, settings.policy).status == "paid"
