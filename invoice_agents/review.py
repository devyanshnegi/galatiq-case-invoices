"""Human review: approve, reject, hold for investigation, and reopen rejected invoices.

One engine behind every interface (dashboard, CLI, MCP), so the controls can't differ between them.

    pending_review ──approve──> (enough approvals?) ──yes──> pay ──> paid
          │   ▲                       └──no──> stays pending_review ("1 of 2 approvals")
          │   └──────────── approve / reject ──────────┐
          ├──hold──> on_hold (investigation) ──────────┘
          └──reject──> rejected ──reopen (policy exception, written reason)──> pending_review

Controls (config/ap_policy.yaml, docs/AP_POLICY.md §2, §4.3, §15):
* each role can approve only up to its limit;
* two *different* people, one of them senior, for large amounts and bank/remit changes;
* reopening a rejection is an exception: two approvers including the CFO; duplicates and
  prompt-injection attacks can never be reopened.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .config import PolicySettings, Settings
from .db import Store
from .models import Invoice

ROLES = {
    "ap_specialist": "AP specialist",
    "department_approver": "Department approver",
    "vp_finance": "VP Finance",
    "cfo": "CFO",
}
REVIEWABLE = ("pending_review", "on_hold")


class ReviewError(ValueError):
    """The action isn't allowed. The message says why, in words a reviewer understands."""


@dataclass
class Requirement:
    approvals: int = 1
    senior_required: bool = False
    senior_roles: tuple[str, ...] = ()
    required_role: str | None = None
    authority_roles: tuple[str, ...] = ()  # roles whose approval limit covers the amount; one approver must hold one
    reasons: list[str] = field(default_factory=list)

    def gaps(self, roles: set[str]) -> list[str]:
        """Role conditions not yet met by approvers holding `roles` (the approval count is checked separately)."""
        out = []
        if self.senior_required and not roles & set(self.senior_roles):
            out.append("a senior approver (" + " or ".join(ROLES[r] for r in self.senior_roles) + ")")
        if self.required_role and self.required_role not in roles:
            out.append(f"approval by the {ROLES.get(self.required_role, self.required_role)}")
        if not roles & set(self.authority_roles) and set(self.authority_roles) != set(ROLES):
            # Only worth saying when the amount rules some roles out (e.g. only the CFO can cover $60K).
            out.append("an approver whose limit covers the amount (" + " or ".join(ROLES[r] for r in self.authority_roles) + ")")
        return out


@dataclass
class ReviewCase:
    """Everything a reviewer needs to decide on one invoice."""

    row: dict[str, Any]
    invoice: Invoice
    result: dict[str, Any]
    history: list[dict[str, Any]]
    requirement: Requirement
    amount_usd: float

    @property
    def invoice_id(self) -> int:
        return self.row["id"]

    @property
    def status(self) -> str:
        return self.row["status"]

    @property
    def is_override(self) -> bool:
        return bool(self.row.get("override_reason"))

    @property
    def approvers(self) -> list[dict[str, Any]]:
        """Approvals that still count: only those since the invoice was last (re)opened."""
        current: list[dict[str, Any]] = []
        for event in self.history:
            if event["action"] == "reopen":
                current = []
            elif event["action"] == "approve":
                current.append(event)
        return current

    @property
    def issue_codes(self) -> set[str]:
        return {i["code"] for i in (self.result.get("validation") or {}).get("issues", [])}

    def blocking_codes(self) -> set[str]:
        return {i["code"] for i in (self.result.get("validation") or {}).get("issues", []) if i["severity"] == "error"}

    def missing(self) -> list[str]:
        """What still stands between this invoice and payment."""
        req, approvers = self.requirement, self.approvers
        out = []
        if len(approvers) < req.approvals:
            out.append(f"{req.approvals - len(approvers)} more approval(s) ({len(approvers)} of {req.approvals})")
        return out + req.gaps({a["role"] for a in approvers})


def _norm(name: str) -> str:
    return " ".join(name.lower().split())


def requirement_for(row: dict[str, Any], result: dict[str, Any], amount_usd: float, policy: PolicySettings) -> Requirement:
    authority = tuple(r for r, limit in policy.approver_limits.items() if limit is None or amount_usd <= limit)
    req = Requirement(senior_roles=tuple(policy.senior_roles), authority_roles=authority)
    codes = {i["code"] for i in (result.get("validation") or {}).get("issues", []) if i["severity"] != "info"}
    if amount_usd >= policy.two_person_threshold:
        req.approvals = max(req.approvals, 2)
        req.senior_required = True
        req.reasons.append(f"${amount_usd:,.2f} is at or above the ${policy.two_person_threshold:,.0f} two-person threshold")
    for code in sorted(codes & set(policy.two_person_codes)):
        req.approvals = max(req.approvals, 2)
        req.senior_required = True
        req.reasons.append(f"{code} always needs two approvers")
    if row.get("override_reason"):
        req.approvals = max(req.approvals, policy.override_approvals)
        req.required_role = policy.override_required_role
        req.reasons.append(f"paying a rejected invoice is a policy exception: {policy.override_approvals} approvers incl. the "
                           f"{ROLES.get(policy.override_required_role, policy.override_required_role)}")
    return req


def load_case(store: Store, invoice_id: int, policy: PolicySettings) -> ReviewCase:
    row = store.get_invoice(invoice_id)
    if row is None:
        raise ReviewError(f"Invoice {invoice_id} doesn't exist")
    result = json.loads(row["result_json"] or "{}")
    invoice = Invoice(**(result.get("invoice") or {}))
    usd = (result.get("validation") or {}).get("total_usd")
    amount = float(usd if usd is not None else (row["total"] or 0))
    return ReviewCase(row=row, invoice=invoice, result=result, history=store.list_approvals(invoice_id),
                      requirement=requirement_for(row, result, amount, policy), amount_usd=amount)


def list_cases(store: Store, policy: PolicySettings, statuses: tuple[str, ...] = REVIEWABLE) -> list[ReviewCase]:
    rows = [r for s in statuses for r in store.list_invoices(s)]
    return [load_case(store, r["id"], policy) for r in sorted(rows, key=lambda r: r["id"])]


def why_cannot_approve(case: ReviewCase, reviewer: str, role: str, policy: PolicySettings) -> str | None:
    """None if this person may approve now; otherwise the reason, for the UI to show."""
    if case.status not in REVIEWABLE:
        return f"invoice is {case.status.replace('_', ' ')}, not awaiting review"
    if not reviewer.strip():
        return "enter your name first"
    if role not in policy.approver_limits:
        return f"unknown role '{role}'"
    limit = policy.approver_limits[role]
    req = case.requirement
    if req.approvals == 1 and role not in req.authority_roles:
        return f"${case.amount_usd:,.2f} is above the {ROLES[role]} limit of ${limit:,.0f}"
    if any(_norm(a["reviewer"]) == _norm(reviewer) for a in case.approvers):
        return "you already approved this invoice - a different person must give the next approval"
    # Two-person cases: anyone may give an earlier approval, but the one that completes the count must
    # leave nothing missing (senior approver, CFO for exceptions, someone with authority for the amount).
    if len(case.approvers) + 1 >= req.approvals and not would_complete(case, role):
        gaps = req.gaps({a["role"] for a in case.approvers} | {role})
        return "this would be the final approval, but payment also needs " + "; ".join(gaps)
    return None


def would_complete(case: ReviewCase, role: str) -> bool:
    """Would one more approval, by someone in `role`, satisfy the requirement (and so trigger payment)?"""
    req = case.requirement
    return len(case.approvers) + 1 >= req.approvals and not req.gaps({a["role"] for a in case.approvers} | {role})


def why_cannot_reopen(case: ReviewCase, policy: PolicySettings) -> str | None:
    if case.status != "rejected":
        return "only rejected invoices can be reopened"
    blocked = case.blocking_codes() & set(policy.not_overridable)
    if blocked:
        return f"{', '.join(sorted(blocked))} can never be paid - not even by a human override"
    if not case.result.get("invoice"):
        return "the document couldn't be read, so there's nothing to approve - process a corrected copy instead"
    return None


# ---- actions ------------------------------------------------------------------------------------


def approve(store: Store, invoice_id: int, *, reviewer: str, role: str, note: str = "", backend, settings: Settings) -> dict:
    """Record an approval; pay once the requirement is met. Returns {'status', 'message', 'payment'?}."""
    case = load_case(store, invoice_id, settings.policy)
    reason = why_cannot_approve(case, reviewer, role, settings.policy)
    if reason:
        raise ReviewError(reason)
    store.add_approval(invoice_id=invoice_id, reviewer=reviewer.strip(), role=role, action="approve", note=note)
    case = load_case(store, invoice_id, settings.policy)
    still = case.missing()
    _audit(store, case, "approved", reviewer, role, note, {"still_needed": still})
    if still:
        if case.status == "on_hold":
            store.update_invoice_status(invoice_id, "pending_review", "escalate", case.row["reasoning"])
        return {"status": "awaiting_approval", "message": "Approval recorded. Still needed: " + "; ".join(still) + "."}

    inv = case.invoice
    payment = backend.pay(invoice_id=invoice_id, invoice_number=inv.invoice_number, vendor=inv.vendor, amount=inv.total,
                          currency=inv.currency, dual_approved=case.requirement.approvals >= 2)
    names = " and ".join(f"{a['reviewer']} ({ROLES.get(a['role'], a['role'])})" for a in case.approvers)
    summary = f"Approved by {names}" + (f": {note}" if note else "") + f" (agent said: {case.row['reasoning']})"
    if payment["status"] in ("success", "skipped"):
        store.update_invoice_status(invoice_id, "paid", "approve", summary)
        _remember(store, case, "approve", note, reviewer)
        return {"status": "paid", "message": f"Paid. Reference {payment.get('reference')}.", "payment": payment}
    if payment["status"] == "declined":
        store.update_invoice_status(invoice_id, "pending_review", "escalate", f"{summary} - bank declined: {payment['reason']}")
        return {"status": "declined", "message": f"Approved, but the bank declined: {payment['reason']}", "payment": payment}
    store.update_invoice_status(invoice_id, "failed", "approve", f"{summary} - payment failed: {payment}")
    return {"status": "failed", "message": f"Payment failed: {payment}", "payment": payment}


def reject(store: Store, invoice_id: int, *, reviewer: str, role: str, note: str = "", settings: Settings) -> dict:
    case = load_case(store, invoice_id, settings.policy)
    if case.status not in REVIEWABLE:
        raise ReviewError(f"invoice is {case.status.replace('_', ' ')}, not awaiting review")
    if not reviewer.strip():
        raise ReviewError("enter your name first")
    store.add_approval(invoice_id=invoice_id, reviewer=reviewer.strip(), role=role, action="reject", note=note)
    store.update_invoice_status(invoice_id, "rejected", "reject",
                                f"Rejected by {reviewer} ({ROLES.get(role, role)})" + (f": {note}" if note else "")
                                + f" (agent said: {case.row['reasoning']})")
    _audit(store, case, "rejected", reviewer, role, note)
    _remember(store, case, "reject", note, reviewer)
    return {"status": "rejected", "message": "Rejected."}


def hold(store: Store, invoice_id: int, *, reviewer: str, role: str, note: str, settings: Settings) -> dict:
    """Park an invoice for investigation (e.g. waiting on a vendor call-back). It stays blocked from payment."""
    case = load_case(store, invoice_id, settings.policy)
    if case.status != "pending_review":
        raise ReviewError("only invoices awaiting review can be put on hold")
    if not note.strip():
        raise ReviewError("say what is being investigated")
    store.add_approval(invoice_id=invoice_id, reviewer=reviewer.strip() or "unknown", role=role, action="hold", note=note)
    store.update_invoice_status(invoice_id, "on_hold", "escalate", case.row["reasoning"])
    _audit(store, case, "on_hold", reviewer, role, note)
    return {"status": "on_hold", "message": "On hold for investigation."}


def reopen(store: Store, invoice_id: int, *, reviewer: str, role: str, reason: str, settings: Settings) -> dict:
    """A human believes a rejected invoice is actually correct. Sends it back to review as a policy exception."""
    case = load_case(store, invoice_id, settings.policy)
    blocked = why_cannot_reopen(case, settings.policy)
    if blocked:
        raise ReviewError(blocked)
    if len(reason.strip()) < 10:
        raise ReviewError("write a justification (at least a sentence) - it goes in the audit trail")
    try:
        store.update_invoice_status(invoice_id, "pending_review", "escalate",
                                    f"Reopened by {reviewer} ({ROLES.get(role, role)}): {reason} (agent rejected it: {case.row['reasoning']})")
    except sqlite3.IntegrityError as exc:
        raise ReviewError("another copy of this invoice is already approved, paid or in review") from exc
    store.set_override(invoice_id, reason)
    store.add_approval(invoice_id=invoice_id, reviewer=reviewer.strip() or "unknown", role=role, action="reopen", note=reason)
    case = load_case(store, invoice_id, settings.policy)
    _audit(store, case, "reopened", reviewer, role, reason)
    return {"status": "pending_review", "message": "Reopened for review. Payment needs: " + "; ".join(case.missing()) + "."}


def _audit(store: Store, case: ReviewCase, event: str, reviewer: str, role: str, note: str, extra: dict | None = None) -> None:
    store.audit(run_id=case.row["run_id"], invoice_number=case.invoice.invoice_number, stage="human_review", event=event,
                detail={"reviewer": reviewer, "role": role, "note": note, **(extra or {})})


def _remember(store: Store, case: ReviewCase, decision: str, note: str, reviewer: str) -> None:
    warnings = [i["code"] for i in (case.result.get("validation") or {}).get("issues", []) if i["severity"] == "warning"]
    store.remember_human_decision(vendor=case.invoice.vendor, invoice_number=case.invoice.invoice_number, issue_codes=warnings,
                                  decision=decision, note=note, reviewer=reviewer)
