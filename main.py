"""Command-line entry point.

    python main.py --invoice_path=data/invoices/invoice_1001.txt
    python main.py --all                       # every file in data/invoices, with a summary
    python main.py --all --provider=offline    # no LLM needed
    python main.py --all --workers 8           # concurrent batch
    python main.py --queue inbox/              # durable job queue: retries, crash recovery
    python main.py --watch inbox/              # process files as they land
    python main.py --pending                   # human review queue
    python main.py --approve 7 --note "PO amendment confirmed"
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

from invoice_agents.agents.payment import resolve_pending
from invoice_agents.config import PROJECT_ROOT, load_settings
from invoice_agents.db import Store
from invoice_agents.graph import NODE_LABELS, InvoicePipeline
from invoice_agents.jobqueue import JobQueue

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


def compact_line(path: Path, state: dict) -> str:
    inv = state.get("invoice") or {}
    status = state.get("status", "failed")
    issues = [i for i in (state.get("validation") or {}).get("issues", []) if i["severity"] in ("error", "warning")]
    why = issues[0]["code"] if issues else ("all checks passed" if status != "failed" else (state.get("error") or "")[:60])
    return (f"  [{STATUS_STYLE.get(status, 'white')}]{status.replace('_', ' '):<15}[/] {path.name:<34} "
            f"{money(inv.get('total'), inv.get('currency', 'USD')):>14}  [dim]{state.get('llm_calls', 0)} LLM · {why}[/]")


def render_throughput(states: list[dict], seconds: float, workers: int) -> None:
    n = len(states)
    if not n or seconds <= 0:
        return
    durations = sorted(sum(e.get("ms") or 0 for e in s.get("trace", [])) for s in states)
    calls = [s.get("llm_calls", 0) for s in states]

    def pct(q: float) -> float:
        return durations[min(n - 1, int(q * n))] / 1000

    console.print(f"[bold]{n} invoices in {seconds:.1f}s with {workers} worker(s) = {60 * n / seconds:,.0f}/min[/] · "
                  f"agent time p50 {pct(0.5):.2f}s, p95 {pct(0.95):.2f}s · {sum(calls) / n:.1f} LLM calls/invoice "
                  f"({sum(1 for c in calls if c == 0)} needed none)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Acme Corp multi-agent invoice processing")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--invoice_path", "--invoice-path", type=Path, help="process a single invoice file")
    target.add_argument("--all", nargs="?", const=PROJECT_ROOT / "data" / "invoices", type=Path, metavar="DIR",
                        help="process every invoice in DIR (default: data/invoices)")
    target.add_argument("--queue", type=Path, metavar="DIR", help="enqueue every invoice in DIR into the durable job queue and drain it")
    target.add_argument("--watch", type=Path, metavar="DIR", help="watch DIR and process invoices as they arrive (Ctrl+C to stop)")
    target.add_argument("--queue-status", action="store_true", help="show job queue counts and recent failures")
    target.add_argument("--pending", action="store_true", help="list invoices awaiting human review")
    target.add_argument("--approve", type=int, metavar="ID", help="human-approve a pending invoice and pay it")
    target.add_argument("--reject", type=int, metavar="ID", help="human-reject a pending invoice")
    parser.add_argument("--note", default="", help="note to attach to a human decision")
    parser.add_argument("--reviewer", default="cli-user", help="name recorded for a human decision")
    parser.add_argument("--provider", help="override LLM_PROVIDER (ollama | xai | openai | anthropic | offline)")
    parser.add_argument("--model", help="override LLM_MODEL")
    parser.add_argument("--workers", type=int, help="invoices processed concurrently (default: WORKERS or 4; --all defaults to 1)")
    parser.add_argument("--backend", choices=["local", "mcp"], help="where ERP + bank tools live (default: TOOLS_BACKEND or local)")
    parser.add_argument("--no-fast-path", action="store_true", help="send every invoice through full LLM deliberation")
    parser.add_argument("--reset", action="store_true", help="clear the ledger (processed invoices/payments/jobs) before running")
    parser.add_argument("--json", action="store_true", help="print the full final state as JSON")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the summary")
    parser.add_argument("-v", "--verbose", action="store_true", help="show info-level findings and debug logs")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(message)s",
                        handlers=[RichHandler(console=console, show_path=False, show_time=False)])
    for noisy in ("httpx", "httpcore", "openai", "anthropic", "pdfminer", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    settings = load_settings(provider=args.provider, model=args.model, workers=args.workers, tools_backend=args.backend,
                             fast_path=False if args.no_fast_path else None)
    store = Store(settings.db_path)
    store.init()
    if args.reset:
        store.reset_ledger()
        console.print("[dim]Ledger cleared.[/]")

    if args.pending:
        show_pending(store)
        return 0
    if args.queue_status:
        queue = JobQueue(store)
        stats = queue.stats()
        console.print(f"queued {stats.queued} · running {stats.running} · done {stats.done} · [red]failed {stats.failed}[/]")
        for job in [j for j in queue.list_jobs() if j["status"] == "failed"][:10]:
            console.print(f"  [red]{Path(job['path']).name}[/]: {job['error']}")
        return 0
    if args.approve or args.reject:
        invoice_id = args.approve or args.reject
        try:
            with InvoicePipeline(settings, store=store) as pipeline:
                result = resolve_pending(store, invoice_id, approve=bool(args.approve), reviewer=args.reviewer, note=args.note,
                                         backend=pipeline.backend)
        except ValueError as exc:
            console.print(f"[red]{exc}[/]")
            return 1
        console.print(f"Invoice {invoice_id}: {'approved' if args.approve else 'rejected'} - {result}")
        return 0

    if not (args.invoice_path or args.all or args.queue or args.watch):
        parser.print_help()
        return 2

    with InvoicePipeline(settings, store=store) as pipeline:
        mode = f"LLM: [bold]{pipeline.llm.label}[/] · tools: [bold]{pipeline.backend.name}[/] · fast path: {'on' if settings.fast_path else 'off'}"
        if pipeline.llm.is_offline:
            mode += "  [yellow](offline rules)[/]"
        console.print(Panel.fit(mode, title="Acme AP Agents", border_style="blue"))

        if args.invoice_path:
            if not args.invoice_path.exists():
                console.print(f"[red]File not found: {args.invoice_path}[/]")
                return 1
            state = run_one(pipeline, args.invoice_path, args)
            return 0 if state.get("status") != "failed" else 1

        if args.all:
            files = sorted(p for p in args.all.iterdir() if p.suffix.lower() in INVOICE_SUFFIXES)
            workers = args.workers or 1  # sequential by default so the detailed per-invoice output stays readable
            if workers == 1:
                started = time.perf_counter()
                results = [(p, run_one(pipeline, p, args)) for p in files]
                seconds = time.perf_counter() - started
            else:
                def show(path: Path, state: dict) -> None:
                    if not args.quiet:
                        console.print(compact_line(path, state))

                batch = pipeline.process_many(files, workers, on_done=show)
                results, seconds = batch.results, batch.seconds
            console.rule()
            if len(results) <= 60:
                render_summary(results)
            render_throughput([s for _, s in results], seconds, workers)
            console.print(f"[dim]{pipeline.llm.usage.calls} LLM call(s), {pipeline.llm.usage.seconds:.1f}s model time · "
                          f"audit log: {settings.log_dir / 'runs.jsonl'}[/]")
            return 0

        queue = JobQueue(store)
        recovered = queue.recover(pipeline)
        if recovered:
            console.print(f"[yellow]Recovered {recovered} job(s) interrupted by a previous crash.[/]")
        done: list[dict] = []

        def finished(job: dict, state: dict) -> None:
            done.append(state)
            if not args.quiet:
                console.print(compact_line(Path(job["path"]), state))

        if args.queue:
            added, skipped = queue.enqueue_dir(args.queue)
            console.print(f"Queued {added} invoice(s)" + (f", skipped {skipped} already seen (same file content)" if skipped else "")
                          + f" · {settings.workers} workers")
            started = time.perf_counter()
            queue.run_workers(pipeline, settings.workers, on_done=finished)
            console.rule()
            render_throughput(done, time.perf_counter() - started, settings.workers)
            stats = queue.stats()
            console.print(f"queue: done {stats.done} · failed {stats.failed}")
            return 0

        console.print(f"Watching [bold]{args.watch}[/] with {settings.workers} workers - drop invoice files in. Ctrl+C to stop.")
        try:
            queue.watch(args.watch, pipeline, settings.workers, on_done=finished,
                        on_enqueue=lambda n: console.print(f"[blue]+{n} new file(s)[/]"))
        except KeyboardInterrupt:
            console.print(f"Stopped. Processed {len(done)} invoice(s) this session.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
