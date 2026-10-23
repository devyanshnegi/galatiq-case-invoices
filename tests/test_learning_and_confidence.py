"""Human-decision memory and confidence-based routing of extractions."""

import contextlib
import io
import json
from dataclasses import replace

from invoice_agents.agents.payment import resolve_pending
from invoice_agents.graph import InvoicePipeline

from .conftest import DATA, FakeLLM


def overpriced(tmp_path, n: int, currency: str = "USD"):
    """A small invoice with an unexplained +20% price: a judgement call the offline rules escalate."""
    path = tmp_path / f"premium_{n}.json"
    path.write_text(json.dumps({
        "invoice_number": f"INV-77{n:02d}", "vendor": {"name": "Consolidated Materials Group"},
        "date": "2026-10-01", "due_date": "2026-10-31", "currency": currency, "payment_terms": "Net 30",
        "line_items": [{"item": "WidgetA", "quantity": 2, "unit_price": 300.0}], "subtotal": 600.0, "total": 600.0,
    }))
    return path


def run(pipeline, path):
    with contextlib.redirect_stdout(io.StringIO()):
        return pipeline.process(path)


def approve(pipeline, state, note="ok"):
    with contextlib.redirect_stdout(io.StringIO()):
        resolve_pending(pipeline.store, state["invoice_id"], approve=True, reviewer="Dana Lee", note=note, backend=pipeline.backend)


def test_repeated_human_approvals_become_precedent(settings, tmp_path):
    with InvoicePipeline(settings) as pipeline:
        first, second = run(pipeline, overpriced(tmp_path, 1)), run(pipeline, overpriced(tmp_path, 2))
        assert first["status"] == second["status"] == "pending_review"
        approve(pipeline, first, "agreed price uplift for Q4")
        approve(pipeline, second, "agreed price uplift for Q4")
        third = run(pipeline, overpriced(tmp_path, 3))
    assert third["status"] == "paid"
    assert any("Approved on precedent" in c for c in third["decision"]["conditions"])


def test_a_single_precedent_or_any_rejection_is_not_enough(settings, tmp_path):
    with InvoicePipeline(settings) as pipeline:
        a, b = run(pipeline, overpriced(tmp_path, 1)), run(pipeline, overpriced(tmp_path, 2))
        approve(pipeline, a)
        with contextlib.redirect_stdout(io.StringIO()):
            resolve_pending(pipeline.store, b["invoice_id"], approve=False, reviewer="Dana Lee", note="price not agreed")
        c = run(pipeline, overpriced(tmp_path, 3))
    assert c["status"] == "pending_review" and "Precedent" in c["decision"]["reasoning"]


def test_precedent_never_overrides_policy_mandated_escalation(settings, tmp_path):
    with InvoicePipeline(settings) as pipeline:
        for n in (1, 2):
            approve(pipeline, run(pipeline, overpriced(tmp_path, n, currency="EUR")), "FX fine")
        assert run(pipeline, overpriced(tmp_path, 3, currency="EUR"))["status"] == "pending_review"


def test_llm_reviewer_sees_precedents(settings, tmp_path):
    settings = replace(settings, fast_path=False)
    with InvoicePipeline(settings) as pipeline:
        approve(pipeline, run(pipeline, overpriced(tmp_path, 1)), "agreed price uplift for Q4")
        llm = FakeLLM({"RiskAssessment": [{"suspicious": False, "signals": []}],
                       "ApprovalDecision": [{"decision": "escalate", "reasoning": "x", "key_factors": [], "conditions": ["check"]}],
                       "Critique": [{"agrees": True, "problems": [], "recommended_decision": "escalate"}]})
        pipeline.ctx.llm = llm
        run(pipeline, overpriced(tmp_path, 2))
    assert "PRECEDENTS" in llm.prompts["ApprovalDecision"][0] and "agreed price uplift for Q4" in llm.prompts["ApprovalDecision"][0]


# ---- confidence routing -------------------------------------------------------------------------

GOOD = {"invoice_number": "INV-1001", "vendor": "Widgets Inc.", "invoice_date": "2026-01-15", "due_date": "2026-02-01",
        "line_items": [{"item": "WidgetA", "quantity": 10, "unit_price": 250}, {"item": "WidgetB", "quantity": 5, "unit_price": 500}],
        "subtotal": 5000, "tax": 0, "total": 5000, "payment_terms": "Net 15"}
WRONG = {**GOOD, "total": 50000}
QUIET = {"RiskAssessment": [{"suspicious": False, "signals": []}],
         "ApprovalDecision": [{"decision": "escalate", "reasoning": "Unverified.", "key_factors": [], "conditions": ["check"]}],
         "Critique": [{"agrees": True, "problems": [], "recommended_decision": "escalate"}]}


def test_unverifiable_extraction_goes_to_a_human(settings):
    llm = FakeLLM({"Invoice": [WRONG], **QUIET})
    with contextlib.redirect_stdout(io.StringIO()):
        state = InvoicePipeline(settings, llm=llm).process(DATA / "invoice_1001.txt")
    assert state["extraction_attempts"] == settings.max_extraction_attempts
    assert state["extraction_confidence"] <= 0.5
    assert "EXTRACTION_UNVERIFIED" in {i["code"] for i in state["validation"]["issues"]}
    assert state["status"] == "pending_review"


def test_second_opinion_model_rescues_a_hard_extraction(settings):
    primary, secondary = FakeLLM({"Invoice": [WRONG], **QUIET}), FakeLLM({"Invoice": [GOOD]})
    pipeline = InvoicePipeline(settings, llm=primary)
    pipeline.ctx.secondary_llm = secondary
    with contextlib.redirect_stdout(io.StringIO()):
        state = pipeline.process(DATA / "invoice_1001.txt")
    assert state["extraction_attempts"] == settings.max_extraction_attempts + 1
    assert any(e["detail"].get("second_opinion") for e in state["trace"] if e["event"] == "extracted")
    assert state["extraction_confidence"] >= 0.6 and state["status"] == "paid"


def test_structured_files_have_full_confidence(settings):
    with contextlib.redirect_stdout(io.StringIO()):
        state = InvoicePipeline(settings).process(DATA / "invoice_1004.json")
    assert state["extraction_confidence"] == 1.0
