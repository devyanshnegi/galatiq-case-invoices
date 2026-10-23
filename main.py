"""Command-line entry point.

    python main.py --invoice_path=data/invoices/invoice_1001.txt
    python main.py --all                       # every file in data/invoices, with a summary
    python main.py --all --provider=offline    # no LLM needed
    python main.py --pending                   # human review queue
    python main.py --approve 7 --note "PO amendment confirmed"
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

from invoice_agents.agents.payment import resolve_pending
from invoice_agents.config import PROJECT_ROOT, load_settings
from invoice_agents.db import Store
from invoice_agents.graph import NODE_LABELS, InvoicePipeline

console = Console()
STATUS_STYLE = {"paid": "green", "rejected": "red", "pending_review": "yellow", "failed": "magenta"}
SEVERITY_STYLE = {"error": "red", "warning": "yellow", "info": "dim"}
INVOICE_SUFFIXES = {".txt", ".pdf", ".json", ".csv", ".xml"}


def money(value, currency="USD") -> str:
    if value is None:
        return "-"
    return f"${value:,.2f}" if currency == "USD" else f"{value:,.2f} {currency}"


def step_line(node: str, update: dict) -> str:
    events = update.get("trace") or []
    ms = sum(e.get("ms") or 0 for e in events)
    detail = ""
    if node == "extract":
        e = events[-1]["detail"] if events else {}
        detail = f"via {e.get('method')}" + (f" ({e.get('model')})" if e.get("model") else "") + f", attempt {e.get('attempt')}"
        if e.get("fallback_reason"):
            detail += " [LLM unavailable - fell back]"
    elif node == "load":
        detail = f"{update.get('source_format', '?')} document" + (" - parsed deterministically" if update.get("extraction_method") == "parser" else "")
    elif node == "self_check":
        problems = update.get("extraction_feedback") or []
        detail = "extraction verified against source" if not problems else f"[yellow]{len(problems)} problem(s): {problems[0]}[/]"
    elif node == "validate":
        v = update.get("validation") or {}
        errs = sum(1 for i in v.get("issues", []) if i["severity"] == "error")
        warns = sum(1 for i in v.get("issues", []) if i["severity"] == "warning")
        detail = f"[red]{errs} error(s)[/], [yellow]{warns} warning(s)[/], risk {v.get('risk_score')}/100"
    elif node == "review":
        d = update.get("draft_decision") or {}
        tools = [c["tool"] for e in events if e["event"] == "tool_calls" for c in e["detail"]["calls"]]
        detail = f"round {update.get('review_round')}: draft [bold]{d.get('decision')}[/] ({d.get('method')})" + (f", tools: {', '.join(tools)}" if tools else "")
    elif node == "critique":
        c = (update.get("critiques") or [{}])[-1]
        detail = "[green]agrees[/]" if c.get("agrees") else f"[yellow]disagrees[/] -> {c.get('recommended_decision')}: {'; '.join(c.get('problems', []))[:120]}"
    elif node == "guardrail":
        d = update.get("decision") or {}
        detail = f"final: [bold]{d.get('decision')}[/]" + (f" [magenta](override: {d['override']})[/]" if d.get("override") else "")
    elif node == "payment":
        p = update.get("payment") or {}
        detail = f"{update.get('status')}" + (f" ref {p['reference']}" if p.get("reference") else "")
    elif node == "failed":
        detail = "[red]document could not be processed[/]"
    return f"  [green]✓[/] {NODE_LABELS.get(node, node):<38} [dim]{ms:>5} ms[/]  {detail}"


def render_result(state: dict, verbose: bool) -> None:
    inv = state.get("invoice") or {}
    if state.get("status") == "failed":
        console.print(Panel(state.get("error") or "unknown error", title="FAILED", border_style="magenta"))
        return

    head = Table.grid(padding=(0, 2))
    head.add_row("[bold]Invoice[/]", inv.get("invoice_number") or "?", "[bold]Vendor[/]", inv.get("vendor") or "[red]missing[/]")
    head.add_row("[bold]Date[/]", inv.get("invoice_date") or "-", "[bold]Due[/]", inv.get("due_date") or f"[red]{inv.get('due_date_raw') or 'missing'}[/]")
    head.add_row("[bold]Total[/]", money(inv.get("total"), inv.get("currency")), "[bold]Terms[/]", inv.get("payment_terms") or "-")
    items = Table("Item", "SKU", "Qty", "Unit price", "Amount", "Note", box=None, header_style="bold dim")
    for li in inv.get("line_items", []):
        items.add_row(li["item"], li.get("sku") or "[red]unknown[/]", f"{li['quantity']:g}" if li.get("quantity") is not None else "-",
                      money(li.get("unit_price"), inv.get("currency")), money(li.get("amount"), inv.get("currency")), li.get("note") or "")
    console.print(Panel.fit(head, title="Extracted", border_style="cyan"))
    console.print(items)

    issues = (state.get("validation") or {}).get("issues", [])
    shown = [i for i in issues if verbose or i["severity"] != "info"]
    if shown:
        table = Table("", "Check", "Finding", box=None, header_style="bold dim")
        for i in shown:
            table.add_row(f"[{SEVERITY_STYLE[i['severity']]}]{i['severity'].upper()}[/]", i["code"], i["message"])
        console.print(table)

    decision = state.get("decision") or {}
    status = state.get("status", "?")
    style = STATUS_STYLE.get(status, "white")
    body = f"[bold]{decision.get('decision', '?').upper()}[/] -> [{style}]{status.replace('_', ' ')}[/]\n\n{decision.get('reasoning', '')}"
    if decision.get("conditions"):
        body += "\n\n[bold]Conditions:[/]\n" + "\n".join(f" • {c}" for c in decision["conditions"])
    critiques = state.get("critiques") or []
    if len(critiques) > 1 or (critiques and not critiques[-1].get("agrees")):
        body += "\n\n[bold]Reflection loop:[/]\n" + "\n".join(
            f" round {c['round']}: audit {'agreed' if c['agrees'] else 'pushed back - ' + '; '.join(c['problems'])[:160]}" for c in critiques
        )
    console.print(Panel(body, title=f"Decision · {(state.get('policy') or {}).get('tier', '?')} tier", border_style=style))


def run_one(pipeline: InvoicePipeline, path: Path, args) -> dict:
    console.rule(f"[bold]{path.name}")
    state: dict = {}
    for node, update, state in pipeline.stream(path):
        if not args.quiet:
            console.print(step_line(node, update))
    if args.json:
        console.print_json(json.dumps({k: v for k, v in state.items() if k != "raw_text"}, default=str))
    elif not args.quiet:
        render_result(state, args.verbose)
    return state


def render_summary(results: list[tuple[Path, dict]]) -> None:
    table = Table(title="Batch summary", header_style="bold")
    for col in ("File", "Invoice", "Vendor", "Amount", "Outcome", "Top reason"):
        table.add_column(col)
    totals: dict[str, float] = {}
    for path, s in results:
        inv = s.get("invoice") or {}
        status = s.get("status", "failed")
        issues = [i for i in (s.get("validation") or {}).get("issues", []) if i["severity"] in ("error", "warning")]
        reason = issues[0]["code"] if issues else "all checks passed"
        usd = (s.get("validation") or {}).get("total_usd") or 0
        totals[status] = totals.get(status, 0) + (usd if usd > 0 else 0)
        table.add_row(path.name, inv.get("invoice_number") or "-", (inv.get("vendor") or "-")[:28], money(inv.get("total"), inv.get("currency", "USD")),
                      f"[{STATUS_STYLE.get(status, 'white')}]{status.replace('_', ' ')}[/]", reason)
    console.print(table)
    counts = {st: sum(1 for _, s in results if s.get("status") == st) for st in STATUS_STYLE}
    console.print(
        f"[green]{counts['paid']} paid ({money(totals.get('paid', 0))})[/] · [red]{counts['rejected']} rejected ({money(totals.get('rejected', 0))} blocked)[/] · "
        f"[yellow]{counts['pending_review']} awaiting human review ({money(totals.get('pending_review', 0))})[/] · [magenta]{counts['failed']} failed[/]"
    )


def show_pending(store: Store) -> None:
    rows = store.list_invoices("pending_review")
    if not rows:
        console.print("[green]No invoices awaiting review.[/]")
        return
    table = Table("ID", "Invoice", "Vendor", "Total", "Why it needs a human", title="Human review queue", header_style="bold")
    for r in rows:
        table.add_row(str(r["id"]), r["invoice_number"] or "-", r["vendor"] or "-", money(r["total"], r["currency"]), r["reasoning"])
    console.print(table)
    console.print("Resolve with: [bold]python main.py --approve ID[/] or [bold]--reject ID[/] (optionally --note \"...\")")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Acme Corp multi-agent invoice processing")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--invoice_path", "--invoice-path", type=Path, help="process a single invoice file")
    target.add_argument("--all", nargs="?", const=PROJECT_ROOT / "data" / "invoices", type=Path, metavar="DIR",
                        help="process every invoice in DIR (default: data/invoices)")
    target.add_argument("--pending", action="store_true", help="list invoices awaiting human review")
    target.add_argument("--approve", type=int, metavar="ID", help="human-approve a pending invoice and pay it")
    target.add_argument("--reject", type=int, metavar="ID", help="human-reject a pending invoice")
    parser.add_argument("--note", default="", help="note to attach to a human decision")
    parser.add_argument("--reviewer", default="cli-user", help="name recorded for a human decision")
    parser.add_argument("--provider", help="override LLM_PROVIDER (ollama | xai | openai | anthropic | offline)")
    parser.add_argument("--model", help="override LLM_MODEL")
    parser.add_argument("--reset", action="store_true", help="clear the ledger (processed invoices/payments) before running")
    parser.add_argument("--json", action="store_true", help="print the full final state as JSON")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the summary")
    parser.add_argument("-v", "--verbose", action="store_true", help="show info-level findings and debug logs")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(message)s",
                        handlers=[RichHandler(console=console, show_path=False, show_time=False)])
    for noisy in ("httpx", "httpcore", "openai", "anthropic", "pdfminer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    settings = load_settings(provider=args.provider, model=args.model)
    store = Store(settings.db_path)
    store.init()
    if args.reset:
        store.reset_ledger()
        console.print("[dim]Ledger cleared.[/]")

    if args.pending:
        show_pending(store)
        return 0
    if args.approve or args.reject:
        invoice_id = args.approve or args.reject
        try:
            result = resolve_pending(store, invoice_id, approve=bool(args.approve), reviewer=args.reviewer, note=args.note)
        except ValueError as exc:
            console.print(f"[red]{exc}[/]")
            return 1
        console.print(f"Invoice {invoice_id}: {'approved' if args.approve else 'rejected'} - {result}")
        return 0

    if not args.invoice_path and not args.all:
        parser.print_help()
        return 2

    pipeline = InvoicePipeline(settings, store=store)
    console.print(Panel.fit(f"LLM: [bold]{pipeline.llm.label}[/]" + ("  [yellow](offline rules - set LLM_PROVIDER to use a model)[/]" if pipeline.llm.is_offline else ""),
                            title="Acme AP Agents", border_style="blue"))

    if args.invoice_path:
        if not args.invoice_path.exists():
            console.print(f"[red]File not found: {args.invoice_path}[/]")
            return 1
        state = run_one(pipeline, args.invoice_path, args)
        return 0 if state.get("status") != "failed" else 1

    files = sorted(p for p in args.all.iterdir() if p.suffix.lower() in INVOICE_SUFFIXES)
    results = [(p, run_one(pipeline, p, args)) for p in files]
    console.rule()
    render_summary(results)
    console.print(f"[dim]{pipeline.llm.usage.calls} LLM call(s), {pipeline.llm.usage.seconds:.1f}s model time · "
                  f"audit log: {settings.log_dir / 'runs.jsonl'}[/]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
