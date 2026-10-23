r"""Approval Agent (VP reviewer) + Critic (internal audit) reflection loop + policy guardrail.

    reviewer --draft--> critic --disagrees--> reviewer (revise, up to N rounds)
                              \--agrees----> guardrail --> payment

The reviewer uses function calling to investigate (policy, inventory, vendor master, history).
The guardrail is plain code: whatever the LLMs conclude, an outcome the policy doesn't allow is
overridden, and an unresolved reviewer/audit disagreement goes to a human.
"""

from __future__ import annotations

import json
import logging
import time

from .. import prompts
from ..llm import LLMError
from ..models import ApprovalDecision, Critique, Invoice, ValidationReport
from ..policy import policy_text
from ..state import AgentContext, InvoiceState, trace_event

log = logging.getLogger(__name__)


def case_file(state: InvoiceState, precedents: list[dict] | None = None) -> str:
    inv = state["invoice"]
    report = state["validation"]

    def section(severity: str) -> list[str]:
        return [f"- {i['code']}: {i['message']}" for i in report["issues"] if i["severity"] == severity] or ["- none"]

    # Info-level notes (e.g. PAST_DUE) are deliberately left out: qwen2.5:7b treated them as blocking
    # even when told otherwise. They affect payment priority, not approval, and stay visible in the UI.
    lines = [
        "INVOICE:",
        json.dumps({k: v for k, v in inv.items() if v not in (None, [], "")}, indent=1),
        f"\nAMOUNT IN USD: {report.get('total_usd')}",
        "\nBLOCKING ERRORS (each one alone means the invoice cannot be paid):", *section("error"),
        "\nWARNINGS (need judgement):", *section("warning"),
        f"\nPOLICY ASSESSMENT: tier={state['policy']['tier']}",
        f"allowed_outcomes = {state['policy']['allowed']}",
        *[f"- {r}" for r in state["policy"]["reasons"]],
    ]
    if precedents:
        lines += ["\nPRECEDENTS (how humans decided this vendor's earlier escalated invoices - weigh them, but the policy wins):",
                  *[f"- {p['decided_at'][:10]} {p['invoice_number']}: {p['decision'].upper()} by {p['reviewer']}"
                    f" (warnings: {', '.join(p['issue_codes']) or 'none'})" + (f" - note: {p['note']}" if p.get("note") else "")
                    for p in precedents]]
    return "\n".join(lines)


# ---- reviewer ---------------------------------------------------------------------------------


def review_node(ctx: AgentContext):
    def review(state: InvoiceState) -> InvoiceState:
        started = time.perf_counter()
        round_no = state.get("review_round", 0) + 1
        critiques = state.get("critiques") or []
        events = []
        decision = None
        if not ctx.llm.is_offline:
            try:
                decision, tool_calls = _llm_review(ctx, state, critiques)
                events.append(trace_event("approval", "tool_calls", None, round=round_no,
                                          calls=[{"tool": c.name, "args": c.arguments} for c in tool_calls]))
            except LLMError as exc:
                log.warning("LLM review failed, using rule-based reviewer: %s", exc)
                events.append(trace_event("approval", "llm_review_failed", None, error=str(exc)))
        method = "llm" if decision else "rules"
        if decision is None:
            inv = Invoice(**state["invoice"])
            decision = rule_based_decision(inv, ValidationReport(**state["validation"]), state["policy"], ctx.store.vendor_precedents(inv.vendor))
        events.append(trace_event("approval", "draft_decision", started, round=round_no, method=method, **decision.model_dump()))
        return {"review_round": round_no, "draft_decision": {**decision.model_dump(), "method": method}, "trace": events}

    return review


def _llm_review(ctx: AgentContext, state: InvoiceState, critiques: list[dict]):
    brief = case_file(state, ctx.store.vendor_precedents(state["invoice"].get("vendor")))
    if critiques:
        last = critiques[-1]
        brief += ("\n\nINTERNAL AUDIT REJECTED YOUR PREVIOUS DRAFT:\n"
                  f"previous decision: {state['draft_decision']['decision']}\n"
                  + "\n".join(f"- {p}" for p in last["problems"])
                  + f"\naudit recommends: {last['recommended_decision']}")
    investigation = ctx.llm.run_tools(system=prompts.REVIEWER, user=brief, tools=ctx.backend.reviewer_tools(), max_steps=4)
    findings = "\n".join(f"- {c.name}({c.arguments}) -> {json.dumps(c.result, default=str)[:400]}" for c in investigation.calls)
    decision = ctx.llm.structured(
        system=prompts.REVIEWER_DECIDE,
        user=f"{brief}\n\nTOOL RESULTS:\n{findings or '- none'}\n\nYOUR INVESTIGATION NOTES:\n{investigation.text}",
        schema=ApprovalDecision,
    )
    return decision, investigation.calls


def rule_based_decision(inv: Invoice, report: ValidationReport, policy: dict, precedents: list[dict] | None = None) -> ApprovalDecision:
    """Deterministic reviewer used offline or when the LLM is unavailable."""
    decision = policy["recommended"]
    factors = [f"{i.code}: {i.message}" for i in report.errors + report.warnings]
    conditions: list[str] = []
    amount = f"${report.total_usd:,.2f}" if report.total_usd is not None else "an unknown amount"

    # Below the VP threshold, a price premium the invoice itself explains (e.g. 'rush order') is acceptable.
    justified = all(
        w.code == "PRICE_ABOVE_CATALOG" and "invoice note" in w.message for w in report.warnings
    )
    if decision == "escalate" and policy["tier"] == "standard" and report.warnings and justified and "approve" in policy["allowed"]:
        decision = "approve"
        conditions.append("Price premium accepted because the invoice documents it; confirm with the requester.")

    # Learned precedent: humans repeatedly approved this vendor with exactly these warnings and never rejected.
    warning_codes = sorted({w.code for w in report.warnings})
    same = [p for p in precedents or [] if p["issue_codes"] == warning_codes]
    if (decision == "escalate" and "approve" in policy["allowed"] and warning_codes
            and len(same) >= 2 and all(p["decision"] == "approve" for p in same)):
        decision = "approve"
        conditions.append(f"Approved on precedent: humans approved {len(same)} earlier {inv.vendor} invoices with the same "
                          f"warnings ({', '.join(warning_codes)}), most recently {same[0]['decided_at'][:10]} by {same[0]['reviewer']}.")

    if decision == "reject":
        reasoning = (f"Rejecting {inv.invoice_number or 'this invoice'} from {inv.vendor or 'an unidentified vendor'} for {amount}: "
                     f"{len(report.errors)} blocking issue(s) - " + "; ".join(i.message for i in report.errors[:3]))
    elif decision == "escalate":
        reasoning = (f"Escalating {inv.invoice_number} ({amount}) to a human approver: " + "; ".join(policy["reasons"]))
        conditions.append("Human approver must confirm before payment.")
        if same:
            verdicts = ", ".join(f"{p['decision']} ({p['decided_at'][:10]})" for p in same[:3])
            reasoning += f" Precedent for this vendor with the same warnings: {verdicts}."
    else:
        reasoning = (f"Approving {inv.invoice_number} from {inv.vendor} for {amount}: all items are in the catalog and within stock, "
                     "the arithmetic reconciles and the due date is valid" + (", and the noted exceptions are documented." if report.warnings else "."))
    return ApprovalDecision(decision=decision, reasoning=reasoning, key_factors=factors[:6] or ["all checks passed"], conditions=conditions)


def policy_decision_node(ctx: AgentContext):
    """Fast path: the policy allows exactly one outcome, so there's nothing for an LLM to deliberate.

    A clean small invoice can only be approved; one with a blocking error can only be rejected.
    Skipping the reviewer/critic here is what makes high volume affordable: LLM calls are spent
    only on invoices where judgement actually changes the outcome.
    """

    def decide(state: InvoiceState) -> InvoiceState:
        started = time.perf_counter()
        inv = Invoice(**state["invoice"])
        decision = rule_based_decision(inv, ValidationReport(**state["validation"]), state["policy"], ctx.store.vendor_precedents(inv.vendor))
        return {"review_round": 0, "draft_decision": {**decision.model_dump(), "method": "policy"},
                "trace": [trace_event("approval", "fast_path_decision", started, decision=decision.decision,
                                      reason=f"policy allows only '{decision.decision}' - no deliberation needed")]}

    return decide


def needs_deliberation(ctx: AgentContext):
    def route(state: InvoiceState) -> str:
        return "fast_path" if ctx.settings.fast_path and len(state["policy"]["allowed"]) == 1 else "deliberate"

    return route


# ---- critic -----------------------------------------------------------------------------------


def critic_node(ctx: AgentContext):
    def critique(state: InvoiceState) -> InvoiceState:
        started = time.perf_counter()
        draft = ApprovalDecision(**{k: v for k, v in state["draft_decision"].items() if k != "method"})
        allowed = state["policy"]["allowed"]
        hard_problems = []
        if draft.decision not in allowed:
            hard_problems.append(f"'{draft.decision}' is not an allowed outcome under the policy; allowed: {allowed}")
        report = ValidationReport(**state["validation"])
        if draft.decision == "approve" and report.warnings and not draft.conditions:
            hard_problems.append("approving despite warnings without stating any conditions or follow-ups")
        # Fact-check: the reasoning may not call something a blocking error when validation found none.
        found = {i.code for i in report.issues}
        blocking = {i.code for i in report.errors}
        info_codes = found - blocking - {i.code for i in report.warnings}
        text = draft.reasoning.lower()
        for code in info_codes:
            mentioned = code.lower() in text or code.replace("_", " ").lower() in text
            if mentioned and "block" in text and not blocking:
                hard_problems.append(f"reasoning treats informational note {code} as a blocking error; there are no blocking errors")

        method = "rules"
        result = Critique(agrees=not hard_problems, problems=hard_problems,
                          recommended_decision=draft.decision if not hard_problems else state["policy"]["recommended"])
        if not ctx.llm.is_offline:
            try:
                llm_critique = ctx.llm.structured(
                    system=prompts.CRITIC,
                    user=f"{case_file(state)}\n\nPOLICY:\n{policy_text(ctx.settings.policy)}\n\nDECISION UNDER REVIEW:\n{draft.model_dump_json(indent=1)}",
                    schema=Critique,
                )
                method = "llm"
                result = Critique(
                    agrees=llm_critique.agrees and not hard_problems,
                    problems=hard_problems + llm_critique.problems,
                    recommended_decision=llm_critique.recommended_decision if llm_critique.recommended_decision in allowed else result.recommended_decision,
                )
            except LLMError as exc:
                log.warning("LLM critique failed, using rule-based critique: %s", exc)
        entry = {**result.model_dump(), "round": state.get("review_round", 1), "method": method}
        return {"critiques": [entry], "trace": [trace_event("approval", "critique", started, **entry)]}

    return critique


def should_revise(ctx: AgentContext):
    def route(state: InvoiceState) -> str:
        last = (state.get("critiques") or [{}])[-1]
        if last.get("agrees") or state.get("review_round", 1) >= ctx.settings.max_review_rounds:
            return "finalize"
        return "revise"

    return route


# ---- guardrail --------------------------------------------------------------------------------


def guardrail_node(ctx: AgentContext):
    def guardrail(state: InvoiceState) -> InvoiceState:
        draft = dict(state["draft_decision"])
        allowed = state["policy"]["allowed"]
        last = (state.get("critiques") or [{}])[-1]
        override = None
        if draft["decision"] not in allowed:
            override = f"policy does not allow '{draft['decision']}' - overridden to '{state['policy']['recommended']}'"
            draft["decision"] = state["policy"]["recommended"]
        elif not last.get("agrees", True) and draft["decision"] != "reject":
            # A human is always a permitted fallback when the agents can't agree.
            override = "reviewer and internal audit did not converge - sent to a human"
            draft["decision"] = "escalate"
        if override:
            draft["reasoning"] = f"{draft['reasoning']} [Guardrail: {override}.]"
        draft["override"] = override
        draft["review_rounds"] = state.get("review_round", 1)
        return {"decision": draft, "trace": [trace_event("approval", "final_decision", None, decision=draft["decision"],
                                                          override=override, rounds=draft["review_rounds"])]}

    return guardrail
