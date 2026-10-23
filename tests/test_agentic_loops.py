"""The agentic behaviours, tested with a scripted LLM so they're deterministic:
extraction self-correction, the reviewer/critic reflection loop, tool calling, guardrails, fallbacks."""

import contextlib
import io
from dataclasses import replace

import pytest

from invoice_agents.config import LLMSettings
from invoice_agents.graph import InvoicePipeline
from invoice_agents.llm import LLMError, OfflineClient, Tool, create_llm
from invoice_agents.llm.base import ToolCall, ToolStep, inline_schema
from invoice_agents.models import ApprovalDecision, Invoice

from .conftest import DATA, FakeLLM

GOOD_1001 = {
    "invoice_number": "INV-1001", "vendor": "Widgets Inc.", "invoice_date": "2026-01-15", "due_date": "2026-02-01",
    "line_items": [{"item": "WidgetA", "quantity": 10, "unit_price": 250}, {"item": "WidgetB", "quantity": 5, "unit_price": 500}],
    "subtotal": 5000, "tax": 0, "total": 5000, "payment_terms": "Net 15",
}
NOT_SUSPICIOUS = {"suspicious": False, "signals": []}
AGREE = {"agrees": True, "problems": [], "recommended_decision": "approve"}


def approve(reason="Clean invoice, within stock, maths reconciles."):
    return {"decision": "approve", "reasoning": reason, "key_factors": ["clean"], "conditions": []}


def run(settings, llm, name):
    with contextlib.redirect_stdout(io.StringIO()):
        return InvoicePipeline(settings, llm=llm).process(DATA / name)


def test_extraction_self_corrects_a_hallucinated_total(settings):
    hallucinated = {**GOOD_1001, "total": 50000}  # not in the document
    llm = FakeLLM({"Invoice": [hallucinated, GOOD_1001], "RiskAssessment": [NOT_SUSPICIOUS],
                   "ApprovalDecision": [approve()], "Critique": [AGREE]})
    state = run(settings, llm, "invoice_1001.txt")
    assert state["extraction_attempts"] == 2
    assert "total 50000" in llm.prompts["Invoice"][1]  # the retry prompt carried the specific problem
    assert state["invoice"]["total"] == 5000 and state["status"] == "paid"


def test_extraction_retries_are_bounded(settings):
    llm = FakeLLM({"Invoice": [{**GOOD_1001, "vendor": "Acme Corp"}], "RiskAssessment": [NOT_SUSPICIOUS],
                   "ApprovalDecision": [approve()], "Critique": [AGREE]})
    state = run(settings, llm, "invoice_1001.txt")
    assert state["extraction_attempts"] == settings.max_extraction_attempts


def test_malformed_json_is_re_asked(settings):
    llm = FakeLLM({"Invoice": ["{not json", GOOD_1001], "RiskAssessment": [NOT_SUSPICIOUS],
                   "ApprovalDecision": [approve()], "Critique": [AGREE]})
    state = run(settings, llm, "invoice_1001.txt")
    assert state["extraction_method"] == "llm" and state["invoice"]["vendor"] == "Widgets Inc."


def test_reflection_loop_revises_after_critique(settings):
    """Reviewer first approves an invoice with a documented warning but no conditions; audit pushes back; reviewer revises."""
    revised = {**approve("Approve: rush premium of +20% on 4 WidgetA is documented on the invoice."), "conditions": ["Confirm rush order with requester"]}
    llm = FakeLLM({
        "Invoice": [{**GOOD_1001, "invoice_number": "INV-1010", "vendor": "Consolidated Materials Group", "invoice_date": "2026-01-27",
                     "due_date": "2026-02-26", "payment_terms": "Net 30", "subtotal": 6700, "tax": 335, "shipping": 150, "total": 7185,
                     "line_items": [{"item": "WidgetA", "quantity": 8, "unit_price": 250, "amount": 2000},
                                    {"item": "WidgetB", "quantity": 4, "unit_price": 500, "amount": 2000},
                                    {"item": "GadgetX", "quantity": 2, "unit_price": 750, "amount": 1500},
                                    {"item": "WidgetA (rush order)", "quantity": 4, "unit_price": 300, "amount": 1200, "note": "rush order"}]}],
        "RiskAssessment": [NOT_SUSPICIOUS],
        "ApprovalDecision": [approve(), revised],
        "Critique": [{"agrees": False, "problems": ["ignores the price warning"], "recommended_decision": "approve"}, AGREE],
    })
    state = run(settings, llm, "invoice_1010.txt")
    assert state["review_round"] == 2
    assert [c["agrees"] for c in state["critiques"]] == [False, True]
    assert "INTERNAL AUDIT REJECTED YOUR PREVIOUS DRAFT" in llm.prompts["ApprovalDecision"][1]
    assert state["status"] == "paid" and state["decision"]["conditions"]


def test_guardrail_overrides_an_llm_that_insists_on_paying_a_blocked_invoice(settings):
    settings = replace(settings, fast_path=False)  # force full deliberation
    """INV-1002 requests 20 GadgetX with 5 in stock. Even if every LLM says approve, it must not be paid."""
    bad = {"invoice_number": "INV-1002", "vendor": "Gadgets Co.", "invoice_date": "2026-01-30", "due_date": "2026-01-30",
           "line_items": [{"item": "GadgetX", "quantity": 20, "unit_price": 750}], "total": 15000, "payment_terms": "Net 30"}
    llm = FakeLLM({"Invoice": [bad], "RiskAssessment": [NOT_SUSPICIOUS], "ApprovalDecision": [approve("Looks fine.")], "Critique": [AGREE]})
    state = run(settings, llm, "invoice_1002.txt")
    assert state["status"] == "rejected"
    assert state["decision"]["override"] and "Guardrail" in state["decision"]["reasoning"]
    assert state["review_round"] == settings.max_review_rounds  # code-level critique kept pushing back


def test_unresolved_disagreement_goes_to_a_human(settings):
    settings = replace(settings, fast_path=False)  # force full deliberation
    llm = FakeLLM({"Invoice": [GOOD_1001], "RiskAssessment": [NOT_SUSPICIOUS], "ApprovalDecision": [approve()],
                   "Critique": [{"agrees": False, "problems": ["vendor history unclear"], "recommended_decision": "escalate"}]})
    state = run(settings, llm, "invoice_1001.txt")
    assert state["status"] == "pending_review" and "did not converge" in state["decision"]["override"]


def test_risk_analyst_signals_become_warnings(settings):
    signal = {"category": "payment_redirection", "evidence": "Remit to new account at Offshore Bank", "severity": "high"}
    llm = FakeLLM({"Invoice": [GOOD_1001], "RiskAssessment": [{"suspicious": True, "signals": [signal]}],
                   "ApprovalDecision": [{"decision": "escalate", "reasoning": "AI risk signal needs a human.", "key_factors": [], "conditions": ["verify"]}],
                   "Critique": [{**AGREE, "recommended_decision": "escalate"}]})
    state = run(settings, llm, "invoice_1001.txt")
    assert any(i["code"] == "AI_RISK_SIGNAL" for i in state["validation"]["issues"])
    assert state["status"] == "pending_review"


def test_reviewer_uses_tools(settings):
    settings = replace(settings, fast_path=False)  # force full deliberation
    steps = [ToolStep(tool_calls=[ToolCall("1", "lookup_inventory", {"item": "WidgetA"}), ToolCall("2", "get_vendor_profile", {"vendor": "Widgets Inc."})]),
             ToolStep(text="Stock fine, vendor approved.")]
    llm = FakeLLM({"Invoice": [GOOD_1001], "RiskAssessment": [NOT_SUSPICIOUS], "ApprovalDecision": [approve()], "Critique": [AGREE]}, tool_steps=steps)
    state = run(settings, llm, "invoice_1001.txt")
    calls = [c for e in state["trace"] if e["event"] == "tool_calls" for c in e["detail"]["calls"]]
    assert [c["tool"] for c in calls] == ["lookup_inventory", "get_vendor_profile"]
    assert "Stock fine" in llm.prompts["ApprovalDecision"][0]
    assert '"stock": 15' in llm.prompts["ApprovalDecision"][0]  # tool result reached the decision prompt


def test_llm_outage_falls_back_to_rules(settings):
    settings = replace(settings, fast_path=False)  # force full deliberation
    class DownLLM(FakeLLM):
        def _complete_json(self, *a, **k):
            raise ConnectionError("connection refused")

        def _tool_step(self, *a, **k):
            raise ConnectionError("connection refused")

    state = run(settings, DownLLM(), "invoice_1001.txt")
    assert state["status"] == "paid" and state["extraction_method"] == "heuristic"
    assert any(e["event"] == "llm_review_failed" for e in state["trace"])


# ---- LLM layer ----------------------------------------------------------------------------------


def test_tool_loop_executes_tools_and_handles_bad_args():
    llm = FakeLLM(tool_steps=[ToolStep(tool_calls=[ToolCall("1", "add", {"a": 1, "b": 2}), ToolCall("2", "add", {"x": 1}), ToolCall("3", "nope", {})]),
                              ToolStep(text="done")])
    add = Tool("add", "add", {"type": "object", "properties": {}}, lambda a, b: a + b)
    result = llm.run_tools(system="", user="go", tools=[add])
    assert result.text == "done"
    assert result.calls[0].result == 3 and "error" in result.calls[1].result and "unknown tool" in result.calls[2].result["error"]


def test_structured_gives_up_after_max_attempts():
    llm = FakeLLM({"ApprovalDecision": ['{"decision": "maybe"}']})
    with pytest.raises(LLMError):
        llm.structured(system="", user="", schema=ApprovalDecision)


def test_schema_is_flattened_for_local_models():
    assert "$ref" not in str(inline_schema(Invoice)) and "$defs" not in inline_schema(Invoice)


def test_factory_falls_back_without_credentials():
    assert isinstance(create_llm(LLMSettings(provider="xai", xai_api_key=None)), OfflineClient)
    with pytest.raises(LLMError):
        create_llm(LLMSettings(provider="anthropic", anthropic_api_key=None, fallback_to_offline=False))
    with pytest.raises(ValueError):
        create_llm(LLMSettings(provider="gpt-banana"))


def test_factory_builds_each_provider_without_network():
    xai = create_llm(LLMSettings(provider="xai", xai_api_key="test-key"))
    assert (xai.provider, xai.model) == ("xai", "grok-3")
    custom = create_llm(replace(LLMSettings(provider="anthropic", anthropic_api_key="test-key"), model="claude-haiku-5-5"))
    assert custom.label == "anthropic:claude-haiku-5-5"


# ---- regressions found running qwen2.5:7b on the samples ----------------------------------------


def test_small_model_cannot_reject_a_clean_invoice_over_an_info_note(settings):
    settings = replace(settings, fast_path=False)  # force full deliberation
    """qwen2.5:7b rejected INV-1001 calling PAST_DUE 'a blocking validation error'. The policy and critic must catch it."""
    bad_reason = {"decision": "reject", "reasoning": "The invoice is past due by 263 days, which is a blocking validation error.",
                  "key_factors": [], "conditions": []}
    llm = FakeLLM({"Invoice": [GOOD_1001], "RiskAssessment": [NOT_SUSPICIOUS],
                   "ApprovalDecision": [bad_reason, approve()], "Critique": [{**AGREE, "recommended_decision": "reject"}, AGREE]})
    state = run(settings, llm, "invoice_1001.txt")
    first = state["critiques"][0]
    assert not first["agrees"]
    assert any("not an allowed outcome" in p for p in first["problems"])
    assert any("informational note PAST_DUE" in p for p in first["problems"])
    assert state["status"] == "paid" and state["review_round"] == 2


def test_case_file_hides_info_notes_from_the_reviewer(settings):
    settings = replace(settings, fast_path=False)  # force full deliberation
    llm = FakeLLM({"Invoice": [GOOD_1001], "RiskAssessment": [NOT_SUSPICIOUS], "ApprovalDecision": [approve()], "Critique": [AGREE]})
    state = run(settings, llm, "invoice_1001.txt")
    prompt = llm.prompts["ApprovalDecision"][0]
    assert "- none" in prompt.split("BLOCKING ERRORS")[1].split("WARNINGS")[0]
    assert "PAST_DUE" not in prompt
    assert any(i["code"] == "PAST_DUE" for i in state["validation"]["issues"])  # still recorded for the UI


def test_risk_signals_that_restate_rules_or_are_minor_do_not_block(settings):
    noisy = {"suspicious": True, "signals": [
        {"category": "other", "evidence": "PAST_DUE: past due by 239 days", "severity": "high"},           # restates a rule
        {"category": "other", "evidence": "Contact Jim at ext 4421 is unusual", "severity": "high"},      # 'other' never blocks
        {"category": "social_engineering", "evidence": "Deliver to dock B", "severity": "low"},           # low never blocks
    ]}
    llm = FakeLLM({"Invoice": [GOOD_1001], "RiskAssessment": [noisy], "ApprovalDecision": [approve()], "Critique": [AGREE]})
    state = run(settings, llm, "invoice_1001.txt")
    ai = [i for i in state["validation"]["issues"] if i["code"] == "AI_RISK_SIGNAL"]
    assert len(ai) == 2 and all(i["severity"] == "info" for i in ai)
    assert state["status"] == "paid"


def test_finalize_recovers_due_date_and_vendor_aside():
    from invoice_agents.agents.ingestion import finalize

    inv = finalize(Invoice(vendor="QuickShip Distributers (formerly FastShip Ltd.)", due_date=None, due_date_raw="2026-01-30",
                           invoice_number="INV 1012"))
    assert (inv.vendor, inv.due_date, inv.invoice_number) == ("QuickShip Distributers", "2026-01-30", "INV-1012")
    assert "formerly FastShip Ltd." in inv.notes


# ---- fast path ----------------------------------------------------------------------------------


def test_fast_path_skips_deliberation_for_clean_invoices(settings):
    llm = FakeLLM({"Invoice": [GOOD_1001], "RiskAssessment": [NOT_SUSPICIOUS]})  # no reviewer/critic scripted
    state = run(settings, llm, "invoice_1001.txt")
    assert state["status"] == "paid" and state["draft_decision"]["method"] == "policy"
    assert "ApprovalDecision" not in llm.prompts and "Critique" not in llm.prompts
    assert llm.usage.calls == 2  # extract + risk review


def test_fast_path_skips_risk_review_when_already_blocked(settings):
    bad = {"invoice_number": "INV-1002", "vendor": "Gadgets Co.", "invoice_date": "2026-01-30", "due_date": "2026-01-30",
           "line_items": [{"item": "GadgetX", "quantity": 20, "unit_price": 750}], "total": 15000, "payment_terms": "Net 30"}
    llm = FakeLLM({"Invoice": [bad]})
    state = run(settings, llm, "invoice_1002.txt")
    assert state["status"] == "rejected" and llm.usage.calls == 1


def test_discretionary_invoices_still_get_full_deliberation(settings):
    llm = FakeLLM({"Invoice": [GOOD_1001], "RiskAssessment": [{"suspicious": True, "signals": [
        {"category": "payment_redirection", "evidence": "Pay to new account", "severity": "high"}]}],
        "ApprovalDecision": [{"decision": "escalate", "reasoning": "Bank change.", "key_factors": [], "conditions": ["verify"]}],
        "Critique": [{**AGREE, "recommended_decision": "escalate"}]})
    state = run(settings, llm, "invoice_1001.txt")
    assert state["draft_decision"]["method"] == "llm" and state["critiques"]
