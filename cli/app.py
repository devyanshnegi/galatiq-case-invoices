"""Command-line interface.

    python main.py process data/invoices/invoice_1001.txt     (also: --invoice_path=..., as in the brief)
    python main.py batch [DIR] [--workers 8] [--dry-run]
    python main.py queue run DIR | queue status | watch DIR
    python main.py review [--rejected] [--as NAME --role ROLE]
    python main.py pending | show ID | approve|reject|hold|reopen ID --note "..."
    python main.py payments [--csv FILE] | policy | doctor [--deep] | reset
"""

from __future__ import annotations

import argparse
import contextlib
import io
import logging
import sqlite3
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from rich.logging import RichHandler
from rich.prompt import Confirm

from invoice_agents import review
from invoice_agents.config import PROJECT_ROOT, Settings, load_settings
from invoice_agents.db import Store
from invoice_agents.graph import InvoicePipeline
from invoice_agents.jobqueue import JobQueue

from . import doctor, review_cli
from .render import (INVOICE_SUFFIXES, banner, compact_line, console, money, render_summary, render_throughput, run_one)

SAMPLES = PROJECT_ROOT / "data" / "invoices"


# ---- dry run ------------------------------------------------------------------------------------------


@contextlib.contextmanager
def dry_run_settings(settings: Settings) -> Iterator[Settings]:
    """Run against a throwaway copy of the ledger: identical logic, real findings and decisions, nothing kept.

    Payments go to the copy's bank table too, so the summary shows exactly what *would* be paid.
    """
    with tempfile.TemporaryDirectory(prefix="acme-dryrun-", ignore_cleanup_errors=True) as tmp:
        copy = Path(tmp) / "ledger.db"
        if settings.db_path.exists():
            src = sqlite3.connect(settings.db_path)
            dst = sqlite3.connect(copy)
            src.backup(dst)  # consistent snapshot, including anything still in the WAL
            src.close()
            dst.close()
        yield replace(settings, db_path=copy, log_dir=Path(tmp) / "logs")


def dry_run_footer(before: dict[int, str], store: Store) -> None:
    rows = store.list_invoices()
    new = [r for r in rows if r["id"] not in before or before[r["id"]] != r["status"]]
    paid = [r for r in new if r["status"] == "paid"]
    console.print(f"[bold black on yellow] DRY RUN [/] {len(new)} invoice(s) affected · would pay {len(paid)} "
                  f"({money(sum(r['total'] or 0 for r in paid if (r['currency'] or 'USD') == 'USD'))}) · "
                  "nothing was written to the real ledger and nothing was paid.")


# ---- argument parsing -----------------------------------------------------------------------------------


LEGACY_TARGETS = {"--invoice_path": "process", "--invoice-path": "process", "--all": "batch", "--queue": "queue",
                  "--watch": "watch", "--queue-status": "queue", "--pending": "pending", "--approve": "approve",
                  "--reject": "reject", "--hold": "hold", "--reopen": "reopen"}


def translate_legacy(argv: list[str]) -> list[str]:
    """Keep the original flag style working: `--invoice_path=X` -> `process X`, `--approve 7` -> `approve 7`, ..."""
    if not argv or not argv[0].startswith("-") or not any(a.split("=")[0] in LEGACY_TARGETS for a in argv):
        return argv
    command: list[str] = []
    rest: list[str] = []
    reset = False
    i = 0
    while i < len(argv):
        flag, _, inline = argv[i].partition("=")
        if flag in LEGACY_TARGETS and not command:
            command = [LEGACY_TARGETS[flag]]
            if flag == "--queue-status":
                command.append("status")
            elif flag == "--queue":
                command.append("run")
            if flag not in ("--pending", "--queue-status"):
                value = inline or (argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("-") else "")
                if value:
                    command.append(value)
                    i += 0 if inline else 1
        elif flag == "--reviewer":
            rest.append("--as" + (f"={inline}" if inline else ""))
        elif flag == "--reset":
            reset = True
        else:
            rest.append(argv[i])
        i += 1
    if reset and command[0] in ("process", "batch", "queue"):
        rest.append("--fresh")
    return command + rest


def build_parser() -> argparse.ArgumentParser:
    engine = argparse.ArgumentParser(add_help=False)
    g = engine.add_argument_group("engine")
    g.add_argument("--provider", help="LLM provider: ollama | xai | openai | anthropic | offline")
    g.add_argument("--model", help="LLM model name")
    g.add_argument("--backend", choices=["local", "mcp"], help="where ERP + bank tools run")
    g.add_argument("--no-fast-path", action="store_true", help="full LLM deliberation on every invoice")

    output = argparse.ArgumentParser(add_help=False)
    o = output.add_argument_group("output")
    o.add_argument("-q", "--quiet", action="store_true", help="summary only")
    o.add_argument("-v", "--verbose", action="store_true", help="show info-level findings and debug logs")
    o.add_argument("--json", action="store_true", help="machine-readable output")

    name, role = review_cli.default_identity()
    who = argparse.ArgumentParser(add_help=False)
    w = who.add_argument_group("reviewer (default from AP_REVIEWER=\"Name\")")
    w.add_argument("--as", "--reviewer", dest="reviewer", default=name, metavar="NAME", help=f"your name (default: {name})")
    w.add_argument("--role", default=role, choices=list(review.ROLES),
                   help="your role (default: from the approver roster in config/ap_policy.yaml)")

    writes = argparse.ArgumentParser(add_help=False)
    writes.add_argument("--dry-run", action="store_true", help="work on a throwaway copy of the ledger; nothing is written or paid")

    fresh = argparse.ArgumentParser(add_help=False)
    fresh.add_argument("--fresh", action="store_true", help="clear the ledger first (asks unless --yes)")
    fresh.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")

    parser = argparse.ArgumentParser(prog="python main.py", description="Acme Corp multi-agent invoice processing.",
                                     epilog="The brief's form also works: python main.py --invoice_path=data/invoices/invoice_1001.txt")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("process", parents=[engine, output, writes, fresh], help="process one invoice with a live agent trace")
    p.add_argument("file", type=Path)
    p = sub.add_parser("batch", parents=[engine, output, writes, fresh], help="process every invoice in a folder")
    p.add_argument("folder", type=Path, nargs="?", default=SAMPLES)
    p.add_argument("--workers", type=int, default=1, help="process N invoices at once (default 1: full detail per invoice)")

    q = sub.add_parser("queue", help="durable job queue: dedupe, retries, crash recovery")
    qs = q.add_subparsers(dest="queue_command", metavar="ACTION", required=True)
    p = qs.add_parser("run", parents=[engine, output, writes, fresh], help="enqueue a folder and process it")
    p.add_argument("folder", type=Path)
    p.add_argument("--workers", type=int, help="concurrent workers (default: WORKERS or 4)")
    qs.add_parser("status", help="queue counts and recent failures")
    p = sub.add_parser("watch", parents=[engine, output], help="process files as they land in a folder (Ctrl+C to stop)")
    p.add_argument("folder", type=Path)
    p.add_argument("--workers", type=int)

    p = sub.add_parser("review", parents=[engine, who], help="review the queue interactively, one invoice at a time")
    p.add_argument("--rejected", action="store_true", help="walk rejected invoices instead (to reopen ones that are actually correct)")
    sub.add_parser("pending", help="list invoices waiting for a human")
    for action, helptext in (("approve", "approve an invoice (pays once enough approvals are in)"),
                             ("reject", "reject an invoice"), ("hold", "put an invoice on hold for investigation"),
                             ("reopen", "reopen a rejected invoice as a policy exception")):
        p = sub.add_parser(action, parents=[engine, who, writes], help=helptext)
        p.add_argument("invoice_id", type=int)
        p.add_argument("--note", default="", help="note or reason (required for reject, hold, reopen)")
    p = sub.add_parser("show", parents=[who], help="everything about one invoice: findings, decision, approvals, payment, trace")
    p.add_argument("invoice_id", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("payments", help="payment register with who authorised each payment")
    p.add_argument("--csv", type=Path, metavar="FILE", help="also write it to a CSV file")
    sub.add_parser("policy", help="the policy in force, where each value comes from, and any mistakes in it")
    p = sub.add_parser("doctor", parents=[engine], help="check setup: model, database, policy, MCP")
    p.add_argument("--deep", action="store_true", help="also call the model and start the MCP servers")
    p = sub.add_parser("reset", help="clear the ledger (invoices, payments, reviews, jobs); inventory is kept")
    p.add_argument("--yes", "-y", action="store_true")
    p = sub.add_parser("reconcile", parents=[engine], help="settle payments a crash left unconfirmed; list unknown outcomes")
    p.add_argument("--older-than", type=float, default=300, metavar="SECONDS", help="only rows untouched this long (default 300)")
    return parser


# ---- commands ----------------------------------------------------------------------------------------------


def _role(args, settings: Settings) -> str:
    """Explicit --role, else the roster's role for this name, else VP Finance (no roster configured)."""
    return getattr(args, "role", None) or review.roster_role(settings.policy, getattr(args, "reviewer", "")) or "vp_finance"


def _settings(args) -> Settings:
    return load_settings(provider=getattr(args, "provider", None), model=getattr(args, "model", None),
                         workers=getattr(args, "workers", None), tools_backend=getattr(args, "backend", None),
                         fast_path=False if getattr(args, "no_fast_path", False) else None)


def _confirm_reset(store: Store, args) -> bool:
    if not getattr(args, "yes", False):
        if not sys.stdin.isatty():  # never wipe the ledger silently from a script or pipe
            console.print("[red]Refusing to clear the ledger without confirmation - add --yes.[/]")
            return False
        if not Confirm.ask(f"Clear {len(store.list_invoices())} processed invoice(s), payments and reviews?", default=False):
            return False
    store.reset_ledger()
    console.print("[dim]Ledger cleared.[/]")
    return True


def cmd_process(args, settings: Settings) -> int:
    paths = [args.file] if args.command == "process" else sorted(p for p in args.folder.iterdir() if p.suffix.lower() in INVOICE_SUFFIXES)
    missing = [p for p in paths if not p.exists()]
    if missing or not paths:
        console.print(f"[red]Not found: {missing[0] if missing else args.folder}[/]")
        return 1
    store = Store(settings.db_path)
    store.init()
    if args.fresh and not _confirm_reset(store, args):
        return 1
    before = {r["id"]: r["status"] for r in store.list_invoices()}
    quiet = contextlib.redirect_stdout(io.StringIO()) if args.dry_run else contextlib.nullcontext()
    with InvoicePipeline(settings, store=store, quiet_payments=args.dry_run) as pipeline:
        banner(pipeline, args.dry_run)
        if args.command == "process":
            with quiet:
                state = run_one(pipeline, paths[0], args)
            code = 1 if state.get("status") == "failed" else 0
        else:
            workers = args.workers or 1
            started = time.perf_counter()
            with quiet:
                if workers == 1:
                    results = [(p, run_one(pipeline, p, args)) for p in paths]
                else:
                    results = pipeline.process_many(paths, workers, on_done=lambda p, s: args.quiet or console.print(compact_line(p, s))).results
            console.rule()
            if len(results) <= 60:
                render_summary(results)
            render_throughput([s for _, s in results], time.perf_counter() - started, workers)
            code = 1 if any(s.get("status") == "failed" for _, s in results) else 0
        if args.dry_run:
            dry_run_footer(before, store)
    return code


def cmd_queue(args, settings: Settings) -> int:
    store = Store(settings.db_path)
    store.init()
    queue = JobQueue(store)
    if args.command == "queue" and args.queue_command == "status":
        stats = queue.stats()
        console.print(f"queued {stats.queued} · running {stats.running} · done {stats.done} · [red]failed {stats.failed}[/]")
        for job in [j for j in queue.list_jobs() if j["status"] == "failed"][:10]:
            console.print(f"  [red]{Path(job['path']).name}[/]: {job['error']}")
        return 0
    if getattr(args, "fresh", False) and not _confirm_reset(store, args):
        return 1
    before = {r["id"]: r["status"] for r in store.list_invoices()}
    done: list[dict] = []

    def finished(job: dict, state: dict) -> None:
        done.append(state)
        if not args.quiet:
            console.print(compact_line(Path(job["path"]), state))

    dry = getattr(args, "dry_run", False)
    with InvoicePipeline(settings, store=store, quiet_payments=dry) as pipeline:
        banner(pipeline, dry)
        recovered = queue.recover(pipeline)
        if recovered:
            console.print(f"[yellow]Recovered {recovered} job(s) interrupted by a previous crash.[/]")
        if args.command == "watch":
            console.print(f"Watching [bold]{args.folder}[/] with {settings.workers} workers - drop invoice files in. Ctrl+C to stop.")
            try:
                queue.watch(args.folder, pipeline, settings.workers, on_done=finished, on_enqueue=lambda n: console.print(f"[blue]+{n} new file(s)[/]"))
            except KeyboardInterrupt:
                console.print(f"Stopped. Processed {len(done)} invoice(s) this session.")
            return 0
        added, skipped = queue.enqueue_dir(args.folder)
        console.print(f"Queued {added} invoice(s)" + (f", skipped {skipped} already seen (same file content)" if skipped else "")
                      + f" · {settings.workers} workers")
        started = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()) if dry else contextlib.nullcontext():
            queue.run_workers(pipeline, settings.workers, on_done=finished)
        console.rule()
        render_throughput(done, time.perf_counter() - started, settings.workers)
        stats = queue.stats()
        console.print(f"queue: done {stats.done} · failed {stats.failed}")
        if dry:
            dry_run_footer(before, store)
    return 1 if stats.failed else 0


def cmd_review_action(args, settings: Settings) -> int:
    store = Store(settings.db_path)
    store.init()
    with InvoicePipeline(settings, store=store, quiet_payments=getattr(args, "dry_run", False)) as pipeline:
        if args.command == "review":
            problem = review.identity_problem(settings.policy, args.reviewer, _role(args, settings))
            if problem:
                console.print(f"[red]{problem}[/] - use --as NAME (or set AP_REVIEWER).")
                return 1
            done = review_cli.interactive(store, settings, pipeline.backend, args.reviewer, _role(args, settings), rejected=args.rejected)
            return 0 if done is not None else 1
        if getattr(args, "dry_run", False):
            console.print("[bold black on yellow] DRY RUN [/] on a copy of the ledger - nothing is recorded or paid")
        with contextlib.redirect_stdout(io.StringIO()) if args.dry_run else contextlib.nullcontext():
            return review_cli.act(args.command, store, settings, pipeline.backend, args.invoice_id, args.reviewer, _role(args, settings), args.note)


def main(argv: list[str] | None = None) -> int:
    argv = translate_legacy(list(sys.argv[1:] if argv is None else argv))
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    logging.basicConfig(level=logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING, format="%(message)s",
                        handlers=[RichHandler(console=console, show_path=False, show_time=False)], force=True)
    for noisy in ("httpx", "httpcore", "openai", "anthropic", "pdfminer", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    settings = _settings(args)
    if args.command == "doctor":
        return doctor.run(settings, deep=args.deep)
    if args.command == "policy":
        return review_cli.policy_show(settings)
    if args.command == "reset":
        store = Store(settings.db_path)
        store.init()
        return 0 if _confirm_reset(store, args) else 1
    if args.command == "reconcile":
        from invoice_agents.agents.payment import settle_unconfirmed

        store = Store(settings.db_path)
        store.init()
        with InvoicePipeline(settings, store=store) as pipeline, contextlib.redirect_stdout(io.StringIO()):
            report = settle_unconfirmed(store, pipeline.backend, older_than_s=args.older_than)
        if not report:
            console.print("[green]Nothing to reconcile.[/]")
        for r in report:
            colour = "green" if r["outcome"] in ("paid", "already paid - marked paid") else "red"
            console.print(f"  [{colour}]{r['outcome']}[/] · {r['invoice_number']} (ID {r['invoice_id']})")
        return 1 if any(r["outcome"] == "needs bank reconciliation" for r in report) else 0
    if args.command in ("pending", "show", "payments"):
        store = Store(settings.db_path)
        store.init()
        if args.command == "pending":
            review_cli.pending(store, settings)
            return 0
        if args.command == "payments":
            review_cli.payments(store, args.csv)
            return 0
        return review_cli.show(store, settings, args.invoice_id, args.reviewer, _role(args, settings), as_json=args.json)

    dry = getattr(args, "dry_run", False)
    with dry_run_settings(settings) if dry else contextlib.nullcontext(settings) as active:
        if args.command in ("process", "batch"):
            return cmd_process(args, active)
        if args.command in ("queue", "watch"):
            return cmd_queue(args, active)
        return cmd_review_action(args, active)


__all__ = ["main", "translate_legacy", "build_parser", "dry_run_settings"]
