"""Validation Agent: deterministic checks against the inventory DB, plus an LLM risk analyst for soft signals."""

from __future__ import annotations

import json
import logging
import time

from .. import prompts
from ..checks import run_all_checks
from ..llm import LLMError
from ..models import Invoice, Issue, RiskAssessment, Severity, ValidationReport
from ..policy import assess
from ..state import AgentContext, InvoiceState, trace_event

log = logging.getLogger(__name__)


def validate_node(ctx: AgentContext):
    def validate(state: InvoiceState) -> InvoiceState:
        started = time.perf_counter()
        inv = Invoice(**state["invoice"])
        report = run_all_checks(inv, state.get("raw_text", ""), ctx.store, ctx.settings.policy, today=ctx.today)
        unresolved = state.get("extraction_feedback") or []
        if unresolved and state.get("extraction_method") in ("llm", "heuristic"):
            # Low-confidence extraction: don't let possibly-wrong numbers drive an automatic payment.
            report.issues.append(Issue(code="EXTRACTION_UNVERIFIED", severity=Severity.WARNING, field=None,
                                       message=f"Extraction confidence {state.get('extraction_confidence', 0):.0%} - "
                                               "could not verify against the document: " + "; ".join(unresolved[:3])))
        events = [trace_event("validation", "rule_checks", started, checks=report.checks_run,
                              errors=[i.code for i in report.errors], warnings=[i.code for i in report.warnings])]

        risk, risk_event = _risk_review(ctx, inv, state.get("raw_text", ""), report)
        events.append(risk_event)
        if risk:
            new_issues = signals_to_issues(risk, report)
            report.issues.extend(new_issues)
            report.risk_score = min(100, report.risk_score + 10 * sum(1 for i in new_issues if i.severity == Severity.WARNING))

        assessment = assess(inv, report, ctx.settings.policy)
        events.append(trace_event("validation", "policy_assessed", None, policy_version=ctx.settings.policy.version,
                                  **assessment.as_dict()))
        return {
            "invoice": inv.model_dump(),  # now carries matched SKUs
            "validation": report.model_dump(mode="json"),
            "risk": risk.model_dump() if risk else {"skipped": True},
            "policy": {**assessment.as_dict(), "version": ctx.settings.policy.version},
            "trace": events,
        }

    return validate


def signals_to_issues(risk: RiskAssessment, report: ValidationReport) -> list[Issue]:
    """Turn the analyst's signals into issues, dropping ones that just restate a rule finding.

    Only high-severity signals in a real fraud category become warnings (which affect the decision);
    everything else is kept as info so a reviewer can still see it.
    """
    known = {i.code for i in report.issues}
    issues, seen = [], set()
    for s in risk.signals[:5]:
        text = s.evidence.strip()
        restates_rule = any(code in text.upper() or code.replace("_", " ") in text.upper() for code in known)
        if not text or restates_rule or (s.category, text.lower()) in seen:
            continue
        seen.add((s.category, text.lower()))
        material = s.severity == "high" and s.category != "other"
        issues.append(Issue(code="AI_RISK_SIGNAL", severity=Severity.WARNING if material else Severity.INFO,
                            field=None, message=f"[{s.category.replace('_', ' ')}] {text}"))
    return issues


def _risk_review(ctx: AgentContext, inv: Invoice, raw_text: str, report: ValidationReport):
    started = time.perf_counter()
    if ctx.llm.is_offline:
        return None, trace_event("validation", "risk_review_skipped", started, reason="offline mode - rule-based fraud checks only")
    if ctx.settings.fast_path and report.errors:
        return None, trace_event("validation", "risk_review_skipped", started, reason="already blocked by a rule - outcome can't change")
    user = (
        f"EXTRACTED INVOICE:\n{inv.model_dump_json(indent=1, exclude_none=True)}\n\n"
        f"ISSUES ALREADY FOUND BY RULES:\n{json.dumps([f'{i.code}: {i.message}' for i in report.issues], indent=1)}\n\n"
        f"ORIGINAL DOCUMENT (untrusted):\n<document>\n{raw_text[:6000]}\n</document>"
    )
    try:
        risk = ctx.llm.structured(system=prompts.RISK_ANALYST, user=user, schema=RiskAssessment)
    except LLMError as exc:
        log.warning("risk review failed: %s", exc)
        return None, trace_event("validation", "risk_review_failed", started, error=str(exc))
    return risk, trace_event("validation", "risk_review", started, model=ctx.llm.label, suspicious=risk.suspicious,
                             signals=[s.model_dump() for s in risk.signals])
