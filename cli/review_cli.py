"""Review from the terminal: interactive walk-through, single actions, and read-only views."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
from collections import Counter
from pathlib import Path
from typing import Callable

from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from invoice_agents import review
from invoice_agents.config import Settings
from invoice_agents.db import Store

from .render import SEVERITY_STYLE, STATUS_STYLE, console, money

Ask = Callable[..., str]


def default_identity() -> tuple[str, str]:
    """AP_REVIEWER="Dana Lee:vp_finance" saves typing --as/--role on every command."""
    raw = os.getenv("AP_REVIEWER", "")
    name, _, role = raw.partition(":")
    return (name.strip() or "cli-user"), (role.strip() or "vp_finance")


def _status(status: str) -> str:
    return f"[{STATUS_STYLE.get(status, 'white')}]{status.replace('_', ' ')}[/]"


def _progress(done: int, needed: int) -> str:
    return "[green]" + "■" * min(done, needed) + "[/][dim]" + "□" * max(needed - done, 0) + f"[/] {done}/{needed}"


def case_panel(case: review.ReviewCase, store: Store, settings: Settings, reviewer: str, role: str, position: str = "") -> Panel:
    inv, row = case.invoice, case.row
    lines = [f"{_status(case.status)}  ·  ID {row['id']}  ·  due {inv.due_date or 'n/a'}"
             + ("  ·  [red]policy exception[/]" if case.is_override else ""),
             "", f"[bold]Why it's here:[/] {row['reasoning']}", ""]
    for i in (case.result.get("validation") or {}).get("issues", []):
        if i["severity"] != "info":
            lines.append(f"  [{SEVERITY_STYLE[i['severity']]}]{i['severity'].upper():<7}[/] {i['code']}: {i['message']}")
    if case.status in review.REVIEWABLE:
        lines += ["", f"[bold]Approvals[/] {_progress(len(case.approvers), case.requirement.approvals)}"]
        lines += [f"  [dim]• {r}[/]" for r in case.requirement.reasons]
        if case.missing():
            lines.append("  Still needed: " + "; ".join(case.missing()))
    precedents = store.vendor_precedents(inv.vendor, limit=3)
    if precedents:
        lines += ["", "[bold]Earlier decisions for this vendor:[/] " + " · ".join(
            f"{p['decision']} {p['invoice_number']} by {p['reviewer']}" for p in precedents)]
    if case.history:
        lines += ["", "[bold]Activity:[/]"] + [f"  {h['decided_at'][:16].replace('T', ' ')} {h['reviewer']} "
                                               f"({review.ROLES.get(h['role'], h['role'])}) {h['action']}"
                                               + (f" - {h['note']}" if h.get("note") else "") for h in case.history[-4:]]
    if case.status in review.REVIEWABLE:
        blocked = review.why_cannot_approve(case, reviewer, role, settings.policy)
        lines.append("")
        if blocked:
            lines.append(f"[yellow]You ({reviewer}, {review.ROLES[role]}) can't approve:[/] {blocked}")
        elif review.would_complete(case, role):
            lines.append(f"[green]Your approval completes the requirement and pays {money(inv.total, inv.currency)}.[/]")
        else:
            lines.append(f"Your approval will be {len(case.approvers) + 1} of {case.requirement.approvals}.")
    title = f"{position} [bold]{inv.invoice_number or '?'}[/] · {inv.vendor or 'unknown vendor'} · [bold]{money(inv.total, inv.currency)}[/]"
    return Panel("\n".join(lines), title=title.strip(), title_align="left",
                 border_style="red" if case.status == "rejected" else STATUS_STYLE.get(case.status, "white"))


def case_details(case: review.ReviewCase) -> None:
    inv = case.invoice
    table = Table("Item", "SKU", "Qty", "Unit price", "Discount", "Amount", "Note", box=None, header_style="bold dim")
    for li in inv.line_items:
        table.add_row(li.item, li.sku or "[red]not in catalog[/]", f"{li.quantity:g}" if li.quantity is not None else "-",
                      money(li.unit_price, inv.currency), money(li.discount, inv.currency) if li.discount else "",
                      money(li.amount, inv.currency), li.note or "")
    console.print(table)
    console.print(f"[dim]Subtotal {money(inv.subtotal, inv.currency)} · tax {money(inv.tax, inv.currency)} · "
                  f"shipping {money(inv.shipping, inv.currency)} · total {money(inv.total, inv.currency)} · "
                  f"terms {inv.payment_terms or 'n/a'} · source {case.row['source_file']}[/]")
    if inv.notes:
        console.print(f"[dim]Notes on the invoice:[/] {inv.notes}")


# ---- interactive ------------------------------------------------------------------------------------


def interactive(store: Store, settings: Settings, backend, reviewer: str, role: str, *, rejected: bool = False,
                ask: Ask = Prompt.ask) -> Counter:
    """Walk the queue (or the rejected list) one invoice at a time. Returns a count of actions taken."""
    statuses = ("rejected",) if rejected else review.REVIEWABLE
    ids = [c.invoice_id for c in review.list_cases(store, settings.policy, statuses=statuses)]
    done: Counter = Counter()
    if not ids:
        console.print("[green]Nothing to review.[/]" if not rejected else "[green]No rejected invoices.[/]")
        return done
    console.print(f"Reviewing as [bold]{reviewer}[/] ({review.ROLES[role]}). {len(ids)} invoice(s). "
                  "[dim]Change identity with --as/--role or AP_REVIEWER=\"Name:role\".[/]")

    for n, invoice_id in enumerate(ids, start=1):
        while True:
            case = review.load_case(store, invoice_id, settings.policy)
            if case.status not in statuses:  # changed by someone else meanwhile
                break
            console.print(case_panel(case, store, settings, reviewer, role, position=f"[{n}/{len(ids)}]"))
            choices = _choices(case, settings, reviewer, role, rejected)
            console.print("  " + "   ".join(f"[bold cyan]{k}[/] {label}" for k, label in choices.items()))
            key = ask("Action", choices=list(choices), default="s", show_choices=False).strip().lower()
            if key == "d":
                case_details(case)
                continue
            if key == "q":
                _summary(done)
                return done
            if key == "s":
                done["skipped"] += 1
                break
            outcome = _do(key, case, store, settings, backend, reviewer, role, ask)
            if outcome is None:  # cancelled (empty reason)
                continue
            done[outcome["status"]] += 1
            colour = {"paid": "green", "rejected": "red", "awaiting_approval": "cyan"}.get(outcome["status"], "yellow")
            console.print(f"  [{colour}]→ {outcome['status'].replace('_', ' ')}[/] {outcome['message']}\n")
            break
    _summary(done)
    return done


def _choices(case, settings, reviewer, role, rejected) -> dict[str, str]:
    if rejected:
        out = {}
        if review.why_cannot_reopen(case, settings.policy) is None:
            out["o"] = "reopen (policy exception)"
        return out | {"d": "details", "s": "skip", "q": "quit"}
    out = {}
    if review.why_cannot_approve(case, reviewer, role, settings.policy) is None:
        complete = review.would_complete(case, role)
        out["a"] = "approve & pay" if complete else f"approve ({len(case.approvers) + 1} of {case.requirement.approvals})"
    out["r"] = "reject"
    if case.status == "pending_review":
        out["h"] = "hold"
    return out | {"d": "details", "s": "skip", "q": "quit"}


def _do(key, case, store, settings, backend, reviewer, role, ask) -> dict | None:
    who = dict(store=store, invoice_id=case.invoice_id, reviewer=reviewer, role=role, settings=settings)
    try:
        if key == "a":
            note = ask("Note (optional)", default="")
            with contextlib.redirect_stdout(io.StringIO()):  # the mock bank prints; the outcome line reports it
                return review.approve(note=note, backend=backend, **who)
        prompt = {"r": "Reason for rejecting", "h": "What needs investigating", "o": "Why is this invoice actually correct"}[key]
        text = ask(f"{prompt} (empty to cancel)", default="").strip()
        if not text:
            return None
        if key == "r":
            return review.reject(note=text, **who)
        if key == "h":
            return review.hold(note=text, **who)
        return review.reopen(reason=text, **who)
    except review.ReviewError as exc:
        console.print(f"  [red]Not allowed:[/] {exc}")
        return None


def _summary(done: Counter) -> None:
    if done:
        console.print("[bold]Session:[/] " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in done.most_common()))


# ---- single actions and views ------------------------------------------------------------------------


def act(action: str, store: Store, settings: Settings, backend, invoice_id: int, reviewer: str, role: str, note: str) -> int:
    who = dict(store=store, invoice_id=invoice_id, reviewer=reviewer, role=role, settings=settings)
    try:
        if action == "approve":
            with contextlib.redirect_stdout(io.StringIO()):
                outcome = review.approve(note=note, backend=backend, **who)
        elif action == "reject":
            outcome = review.reject(note=note, **who)
        elif action == "hold":
            outcome = review.hold(note=note, **who)
        else:
            outcome = review.reopen(reason=note, **who)
    except review.ReviewError as exc:
        console.print(f"[red]Not allowed:[/] {exc}")
        return 1
    console.print(f"[bold]{outcome['status'].replace('_', ' ')}[/] - {outcome['message']}")
    return 0


def pending(store: Store, settings: Settings) -> None:
    cases = review.list_cases(store, settings.policy)
    if not cases:
        console.print("[green]No invoices awaiting review.[/]")
        return
    table = Table("ID", "Status", "Invoice", "Vendor", "Total", "Approvals", "Still needed", "Why",
                  title="Human review queue", header_style="bold")
    for c in cases:
        r = c.row
        table.add_row(str(r["id"]), _status(c.status), r["invoice_number"] or "-", r["vendor"] or "-", money(r["total"], r["currency"]),
                      _progress(len(c.approvers), c.requirement.approvals), "; ".join(c.missing()) or "-", (r["reasoning"] or "")[:90])
    console.print(table)
    console.print("Review interactively with [bold]python main.py review[/], or act directly: "
                  "[bold]approve | reject | hold | reopen ID --note \"...\"[/]")


def show(store: Store, settings: Settings, invoice_id: int, reviewer: str, role: str, as_json: bool = False) -> int:
    try:
        case = review.load_case(store, invoice_id, settings.policy)
    except review.ReviewError as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    trace = store.audit_for_run(case.row["run_id"])
    payment = store.payment_for_invoice(invoice_id)
    if as_json:
        console.print_json(json.dumps({"invoice": case.row | {"result_json": case.result}, "approvals": case.history,
                                       "payment": payment, "trace": trace}, default=str))
        return 0
    console.print(case_panel(case, store, settings, reviewer, role))
    case_details(case)
    if payment:
        console.print(f"[green]Payment[/] {payment['reference']} · {money(payment['amount'], payment['currency'])} · "
                      f"{payment['status']} · {payment['paid_at'][:16].replace('T', ' ')}")
    if trace:
        table = Table("Stage", "Event", "ms", "Detail", title="Agent trace", box=None, header_style="bold dim", title_justify="left")
        for e in trace:
            detail = json.loads(e["detail"] or "{}")
            ms = detail.pop("ms", None)
            table.add_row(e["stage"], e["event"], str(ms or ""), json.dumps(detail, default=str)[:110])
        console.print(table)
    return 0


def payments(store: Store, csv_path: Path | None = None) -> None:
    rows = [p for p in store.list_payments() if p["status"] == "success"]
    approvers: dict[int, list[str]] = {}
    for a in reversed(store.all_approvals()):
        if a["action"] == "approve":
            approvers.setdefault(a["invoice_id"], []).append(f"{a['reviewer']} ({review.ROLES.get(a['role'], a['role'])})")
    table = Table("Paid at", "Reference", "Invoice", "Vendor", "Amount", "Authorised by", title="Payment register", header_style="bold")
    out = []
    for p in rows:
        who = " + ".join(approvers.get(p["invoice_id"], [])) or "agents (auto-approved)"
        table.add_row(p["paid_at"][:16].replace("T", " "), p["reference"], p["invoice_number"] or "-", p["vendor"] or "-",
                      money(p["amount"], p["currency"]), who)
        out.append({**{k: p[k] for k in ("paid_at", "reference", "invoice_number", "vendor", "amount", "currency")}, "authorised_by": who})
    console.print(table)
    usd = sum(p["amount"] or 0 for p in rows if (p["currency"] or "USD") == "USD")
    console.print(f"{len(rows)} payment(s), {money(usd)} in USD · {sum(1 for p in rows if p['invoice_id'] in approvers)} human-approved")
    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["paid_at", "reference", "invoice_number", "vendor", "amount", "currency", "authorised_by"])
            writer.writeheader()
            writer.writerows(out)
        console.print(f"Wrote {csv_path}")


ENV_OVERRIDES = {"VP_REVIEW_THRESHOLD": "approval.vp_review_threshold", "CFO_THRESHOLD": "approval.cfo_threshold",
                 "PRICE_VARIANCE_TOLERANCE": "matching.price_variance_tolerance", "BANK_SINGLE_PAYMENT_LIMIT": "payments.single_payment_limit",
                 "FAST_PATH": "automation.fast_path"}


def policy_show(settings: Settings) -> int:
    from invoice_agents.policy import policy_problems

    p = settings.policy
    overridden = {key for env, key in ENV_OVERRIDES.items() if os.getenv(env)}
    table = Table("Setting", "Value", "Source", title=f"Policy {p.policy_id} v{p.version} · {settings.policy_file or 'built-in defaults'}",
                  header_style="bold", title_justify="left")

    def row(key: str, value) -> None:
        table.add_row(key, str(value), "[yellow].env override[/]" if key in overridden else "policy file" if settings.policy_file else "default")

    row("approval.vp_review_threshold", money(p.vp_review_threshold))
    row("approval.cfo_threshold", money(p.cfo_threshold))
    row("approval.approver_limits", ", ".join(f"{review.ROLES.get(r, r)} {'unlimited' if v is None else money(v)}" for r, v in p.approver_limits.items()))
    row("human_review.two_person_approval_at", money(p.two_person_threshold))
    row("human_review.two_person_for_codes", ", ".join(p.two_person_codes))
    row("human_review.override", f"{p.override_approvals} approvers incl. {review.ROLES.get(p.override_required_role, p.override_required_role)}; "
                                 f"never: {', '.join(p.not_overridable)}")
    row("matching.price_variance_tolerance", f"{p.price_variance_tolerance:.0%}")
    row("anti_splitting", f"{p.split_window_days} days, invoices >= {p.split_min_share:.0%} of VP threshold")
    row("currency", f"base {p.base_currency}; " + ", ".join(f"{c} {r}" for c, r in p.fx_rates.items()))
    row("payments.single_payment_limit", money(settings.bank_single_payment_limit))
    row("payments.dual_approved_payment_limit", money(settings.bank_dual_approved_limit))
    row("automation.fast_path", settings.fast_path)
    row("automation.precedent_min_approvals", p.precedent_min_approvals)
    console.print(table)
    console.print("[bold]Always escalate:[/] " + ", ".join(p.always_escalate))
    problems = policy_problems(settings)
    for problem in problems:
        console.print(f"[red]✗[/] {problem}")
    if not problems:
        console.print("[green]✓ policy is consistent[/]")
    return 1 if problems else 0
