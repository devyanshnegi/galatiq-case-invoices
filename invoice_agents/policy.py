"""Approval policy: which outcomes are *allowed* for an invoice, before any LLM gets a say.

The LLM reviewer reasons within these bounds; the guardrail enforces them afterwards. That way a
model that hallucinates, or is talked into "approve" by a persuasive invoice, still can't pay it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import PolicySettings
from .models import Invoice, ValidationReport

# Warnings that always need a human, regardless of amount.
ESCALATE_CODES = {
    "FOREIGN_CURRENCY": "foreign-currency invoices need treasury to confirm the FX rate",
    "REVISED_INVOICE": "a revision of an invoice we already acted on needs AP to reconcile against the PO",
    "INVOICE_NUMBER_COLLISION": "an invoice number reused by a different vendor needs investigation",
    "VENDOR_ADDRESS_MISMATCH": "a changed remit address is a classic payment-redirection fraud pattern",
    "VENDOR_NAME_VARIANT": "a look-alike vendor name must be confirmed as the same legal entity",
    "UNKNOWN_VENDOR": "first payment to a vendor not in the vendor master needs onboarding/KYC",
}


@dataclass
class PolicyAssessment:
    tier: str  # standard | vp | cfo
    allowed: list[str]
    recommended: str
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"tier": self.tier, "allowed": self.allowed, "recommended": self.recommended, "reasons": self.reasons}


def assess(inv: Invoice, report: ValidationReport, policy: PolicySettings) -> PolicyAssessment:
    amount = report.total_usd if report.total_usd is not None else (inv.total or 0)
    tier = "cfo" if amount >= policy.cfo_threshold else "vp" if amount >= policy.vp_review_threshold else "standard"
    reasons: list[str] = []

    if report.errors:
        reasons.append(f"{len(report.errors)} blocking validation error(s): " + ", ".join(sorted({i.code for i in report.errors})))
        return PolicyAssessment(tier, ["reject"], "reject", reasons)

    escalations = [f"{i.code}: {ESCALATE_CODES[i.code]}" for i in report.warnings if i.code in ESCALATE_CODES]
    if escalations:
        reasons += escalations
        return PolicyAssessment(tier, ["escalate", "reject"], "escalate", reasons)

    if tier == "cfo":
        reasons.append(f"${amount:,.2f} is at or above the ${policy.cfo_threshold:,.0f} CFO threshold - human sign-off required")
        return PolicyAssessment(tier, ["escalate", "reject"], "escalate", reasons)

    if tier == "vp":
        reasons.append(f"${amount:,.2f} is at or above the ${policy.vp_review_threshold:,.0f} VP threshold - additional scrutiny")
        if report.warnings:
            reasons.append("VP-tier invoices may only be auto-approved with zero warnings")
            return PolicyAssessment(tier, ["escalate", "reject"], "escalate", reasons)
        # Clean: nothing to reject on, but the VP may still ask for a human look at a large amount.
        return PolicyAssessment(tier, ["approve", "escalate"], "approve", reasons)

    if report.warnings:
        reasons.append(f"{len(report.warnings)} warning(s) need reviewer judgement: " + ", ".join(sorted({i.code for i in report.warnings})))
        return PolicyAssessment(tier, ["approve", "escalate", "reject"], "escalate", reasons)
    # Clean and small: straight-through. A rejection needs a finding to point at.
    reasons.append("clean invoice under the VP threshold - no errors or warnings, so it is paid straight through")
    return PolicyAssessment(tier, ["approve"], "approve", reasons)


def policy_text(policy: PolicySettings) -> str:
    lines = [
        "ACME CORP ACCOUNTS PAYABLE APPROVAL POLICY",
        "1. Any blocking validation error (unknown item, insufficient stock, bad arithmetic, missing/invalid due date, "
        "duplicate, non-positive amount) => REJECT. No exceptions.",
        f"2. Invoices >= ${policy.vp_review_threshold:,.0f} get VP-level scrutiny: approve only if there are zero warnings.",
        f"3. Invoices >= ${policy.cfo_threshold:,.0f} are never auto-approved: ESCALATE to a human.",
        "4. These warnings always require a human (ESCALATE or REJECT, never APPROVE):",
        *[f"   - {code}: {why}" for code, why in ESCALATE_CODES.items()],
        "   Info-level notes (e.g. PAST_DUE, SPLIT_LINES, PRICE_BELOW_CATALOG) are NOT problems and are never a reason to reject.",
        "   An invoice with no errors and no warnings below the VP threshold is approved; it may not be rejected.",
        "5. Below the VP threshold, other warnings (e.g. price above catalog) are the reviewer's judgement: approve only "
        "if the invoice itself documents a legitimate reason; otherwise escalate.",
        "6. Social-engineering signals (urgency, threats, wire-transfer requests) weigh heavily towards REJECT.",
        "7. When in doubt, ESCALATE - a delayed payment is cheaper than a fraudulent one.",
    ]
    return "\n".join(lines)
