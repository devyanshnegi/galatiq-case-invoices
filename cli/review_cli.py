"""Review from the terminal: interactive walk-through, single actions, and read-only views."""

from __future__ import annotations

import contextlib
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


def default_identity() -> tuple[str, str | None]:
    """AP_REVIEWER="Dana Lee" (or "Dana Lee:vp_finance") saves typing --as on every command.
    With an approver roster the role comes from the roster, so it can be left out."""
    raw = os.getenv("AP_REVIEWER", "")
    name, _, role = raw.partition(":")
    return (name.strip() or "cli-user"), (role.strip() or None)


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


def interactive(store: Store, settings: Settings, backend, reviewer: str, role: str, *, ask: Ask = Prompt.ask) -> Counter:
    """Walk the review queue one invoice at a time. Returns a count of actions taken."""
    statuses = review.REVIEWABLE
    ids = [c.invoice_id for c in review.list_cases(store, settings.policy, statuses=statuses)]
    done: Counter = Counter()
    if not ids:
        console.print("[green]Nothing to review.[/]")
        return done
    console.print(f"Reviewing as [bold]{reviewer}[/] ({review.ROLES[role]}). {len(ids)} invoice(s). "
                  "[dim]Change identity with --as NAME or AP_REVIEWER=\"Name\".[/]")

    for n, invoice_id in enumerate(ids, start=1):
        while True:
            case = review.load_case(store, invoice_id, settings.policy)
            if case.status not in statuses:  # changed by someone else meanwhile
                break
            console.print(case_panel(case, store, settings, reviewer, role, position=f"[{n}/{len(ids)}]"))
            choices = _choices(case, settings, reviewer, role)
            console.print("  " + "   ".join(f"[bold cyan]{k}[/] {label}" for k, label in choices.items()))
            try:
                key = ask("Action", choices=list(choices), default="s", show_choices=False).strip().lower()
            except (EOFError, KeyboardInterrupt):  # input ended (Ctrl+C, Ctrl+Z, or no terminal): stop cleanly
                console.print("\n[dim]Review ended.[/]")
                _summary(done)
                return done
            if key == "d":
                case_details(case)
                continue
            if key == "q":
                _summary(done)
                return done
            if key == "s":
                done["skipped"] += 1
                break
            try:
                outcome = _do(key, case, store, settings, backend, reviewer, role, ask)
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Review ended - that action was not recorded.[/]")
                _summary(done)
                return done
            if outcome is None:  # cancelled (empty reason)
                continue
            done[outcome["status"]] += 1
            colour = {"paid": "green", "rejected": "red", "awaiting_approval": "cyan"}.get(outcome["status"], "yellow")
            console.print(f"  [{colour}]→ {outcome['status'].replace('_', ' ')}[/] {outcome['message']}\n")
            break
    _summary(done)
    return done


def _choices(case, settings, reviewer, role) -> dict[str, str]:
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
        prompt = {"r": "Reason for rejecting", "h": "What needs investigating"}[key]
        text = ask(f"{prompt} (empty to cancel)", default="").strip()
        if not text:
            return None
        if key == "r":
            return review.reject(note=text, **who)
        return review.hold(note=text, **who)
    except review.ReviewError as exc:
        console.print(f"  [red]Not allowed:[/] {exc}")
        return None


def _summary(done: Counter) -> None:
    if done:
        console.print("[bold]Session:[/] " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in done.most_common()))


# ---- single actions and views ------------------------------------------------------------------------


def resolve_ref(store: Store, ref: str, prefer: tuple[str, ...] = ()) -> int:
    """Turn what a person typed into a ledger ID. Accepts the ledger ID shown by `pending` (e.g. 18)
    or the invoice number (INV-1006, 'inv 1006', 1006). Raises LookupError with a helpful message."""
    from invoice_agents.normalize import normalize_invoice_number

    ref = str(ref).strip()
    number = normalize_invoice_number(ref)
    rows = store.find_prior_invoices(number, None) if number else []
    if ref.isdigit():
        by_id = store.get_invoice(int(ref))
        # A bare number could be a ledger ID or an invoice number: never guess when it's both.
        if by_id and rows and by_id["invoice_number"] != number:
            ids = ", ".join(str(r["id"]) for r in rows)
            raise LookupError(f"'{ref}' is both ledger ID {ref} ({by_id['invoice_number']}) and invoice {number} (ID {ids}). "
                              f"Type {number} for the invoice, or the ID of the one you mean.")
        if by_id:
            return int(ref)
    if not rows:
        raise LookupError(f"No invoice with ledger ID or invoice number '{ref}'. "
                          "See `python main.py pending` for IDs, or process invoices first with `python main.py batch`.")
    candidates = [r for r in rows if r["status"] in prefer] or rows if prefer else rows
    if len(candidates) == 1:
        return candidates[0]["id"]
    if not prefer:
        # Just looking: show the most relevant entry (in review > paid > rejected/failed), unless that's a tie.
        rank = {"pending_review": 0, "on_hold": 0, "approved": 1, "paid": 1, "rejected": 2, "failed": 3}
        best = min(rank.get(r["status"], 4) for r in candidates)
        top = [r for r in candidates if rank.get(r["status"], 4) == best]
        if len(top) == 1:
            others = ", ".join(f"ID {r['id']} ({r['status'].replace('_', ' ')})" for r in candidates if r is not top[0])
            console.print(f"[dim]{number} has other ledger entries too: {others}. Use the ID to see one of those.[/]")
            return top[0]["id"]
    options = "; ".join(f"ID {r['id']} ({r['status'].replace('_', ' ')}, {Path(r['source_file'] or '?').name})" for r in candidates)
    raise LookupError(f"{number} matches more than one ledger entry: {options}. Use the ID.")


PREFER_FOR = {"approve": review.REVIEWABLE, "reject": review.REVIEWABLE}


def act(action: str, store: Store, settings: Settings, backend, invoice_ref, reviewer: str, role: str, note: str) -> int:
    try:
        invoice_id = resolve_ref(store, invoice_ref, PREFER_FOR.get(action, ()))
    except LookupError as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    who = dict(store=store, invoice_id=invoice_id, reviewer=reviewer, role=role, settings=settings)
    try:
        if action == "approve":
            with contextlib.redirect_stdout(io.StringIO()):
                outcome = review.approve(note=note, backend=backend, **who)
        else:
            outcome = review.reject(note=note, **who)
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
                  "[bold]approve | reject INVOICE --note \"...\"[/]")


def show(store: Store, settings: Settings, invoice_ref, reviewer: str, role: str) -> int:
    try:
        invoice_id = resolve_ref(store, invoice_ref)
        case = review.load_case(store, invoice_id, settings.policy)
    except (LookupError, review.ReviewError) as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    trace = store.audit_for_run(case.row["run_id"])
    payment = store.payment_for_invoice(invoice_id)
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


def payments(store: Store) -> None:
    rows = [p for p in store.list_payments() if p["status"] == "success"]
    approvers: dict[int, list[str]] = {}
    for a in reversed(store.all_approvals()):
        if a["action"] == "approve":
            approvers.setdefault(a["invoice_id"], []).append(f"{a['reviewer']} ({review.ROLES.get(a['role'], a['role'])})")
    table = Table("Paid at", "Reference", "Invoice", "Vendor", "Amount", "Authorised by", title="Payment register", header_style="bold")
    for p in rows:
        who = " + ".join(approvers.get(p["invoice_id"], [])) or "agents (auto-approved)"
        table.add_row(p["paid_at"][:16].replace("T", " "), p["reference"], p["invoice_number"] or "-", p["vendor"] or "-",
                      money(p["amount"], p["currency"]), who)
    console.print(table)
    usd = sum(p["amount"] or 0 for p in rows if (p["currency"] or "USD") == "USD")
    console.print(f"{len(rows)} payment(s), {money(usd)} in USD · {sum(1 for p in rows if p['invoice_id'] in approvers)} human-approved")
