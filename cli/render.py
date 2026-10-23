"""Terminal rendering for the CLI (rich)."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from invoice_agents.graph import NODE_LABELS, InvoicePipeline

console = Console()
STATUS_STYLE = {"paid": "green", "rejected": "red", "pending_review": "yellow", "on_hold": "blue", "failed": "magenta"}
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


def render_result(state: dict) -> None:
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
    shown = [i for i in issues if i["severity"] != "info"]
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
    if not args.quiet:
        render_result(state)
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


def banner(pipeline: InvoicePipeline, dry_run: bool = False) -> None:
    s = pipeline.settings
    mode = (f"LLM: [bold]{pipeline.llm.label}[/] · tools: [bold]{pipeline.backend.name}[/] · fast path: {'on' if s.fast_path else 'off'}"
            f" · policy {s.policy.policy_id} v{s.policy.version}")
    if pipeline.llm.is_offline:
        mode += "  [yellow](offline rules)[/]"
    if dry_run:
        mode += "\n[bold black on yellow] DRY RUN [/] working on a throwaway copy of the ledger - nothing is written, nothing is paid"
    console.print(Panel.fit(mode, title="Acme AP Agents", border_style="yellow" if dry_run else "blue"))
