"""Command-line interface. Full reference: docs/CLI.md.

    python main.py --invoice_path=data/invoices/invoice_1001.txt     (the brief's form = `process FILE`)
    python main.py batch [FOLDER] [--workers N] [--dry-run] [-q]
    python main.py review | approve INVOICE | reject INVOICE --note "..."
    python main.py pending | show INVOICE | payments | reset | doctor
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
from invoice_agents.agents.payment import settle_unconfirmed
from invoice_agents.graph import InvoicePipeline

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


def translate_legacy(argv: list[str]) -> list[str]:
    """The brief's form, `--invoice_path=FILE` (or `--invoice_path FILE`), means `process FILE`."""
    if not argv or argv[0].split("=")[0] not in ("--invoice_path", "--invoice-path"):
        return argv
    flag, _, inline = argv[0].partition("=")
    if inline:
        return ["process", inline, *argv[1:]]
    return ["process", *argv[1:]]


def build_parser() -> argparse.ArgumentParser:
    name, _ = review_cli.default_identity()
    invoice_help = "invoice number (e.g. INV-1006) or the ID shown by `pending`"

    parser = argparse.ArgumentParser(prog="python main.py", description="Acme Corp multi-agent invoice processing.",
                                     epilog="Full reference: docs/CLI.md. The brief's form also works: "
                                            "python main.py --invoice_path=data/invoices/invoice_1001.txt")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name_: str, help_: str, *, provider=False, workers=False, dry_run=False, quiet=False, who=False):
        p = sub.add_parser(name_, help=help_)
        if provider:
            p.add_argument("--provider", help="LLM to use: ollama, xai, anthropic, openai or offline (default: from .env)")
        if workers:
            p.add_argument("--workers", type=int, metavar="N", help="invoices processed at the same time")
        if dry_run:
            p.add_argument("--dry-run", action="store_true", help="work on a throwaway copy of the ledger; nothing is written or paid")
        if quiet:
            p.add_argument("-q", "--quiet", action="store_true", help="summary only")
        if who:
            p.add_argument("--as", dest="reviewer", default=name, metavar="NAME",
                           help=f"your name from the approver roster (default: AP_REVIEWER, currently '{name}')")
        p.set_defaults(quiet=False, dry_run=False, workers=None, provider=None, reviewer=name)
        return p

    p = add("process", "process one invoice with a live agent trace", provider=True, dry_run=True)
    p.add_argument("file", type=Path)
    p = add("batch", "process every invoice in a folder", provider=True, workers=True, dry_run=True, quiet=True)
    p.add_argument("folder", type=Path, nargs="?", default=SAMPLES, help="default: data/invoices")

    add("review", "review the queue one invoice at a time: approve, reject or hold", who=True)
    add("pending", "list invoices waiting for a person")
    p = add("show", "everything about one invoice: findings, decision, approvals, payment, trace")
    p.add_argument("invoice_id", metavar="INVOICE", help=invoice_help)
    for action, helptext in (("approve", "approve an invoice (pays once enough approvals are in)"), ("reject", "reject an invoice")):
        p = add(action, helptext, who=True)
        p.add_argument("invoice_id", metavar="INVOICE", help=invoice_help)
        p.add_argument("--note", default="", help="note or reason (required for reject)")

    add("payments", "payment register: every payment and who authorised it")
    p = add("reset", "clear the ledger (invoices, payments, reviews, jobs); inventory and vendors are kept")
    p.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    add("doctor", "check the setup: policy, database, model", provider=True)
    return parser


# ---- commands ----------------------------------------------------------------------------------------------


def _role(args, settings: Settings) -> str:
    """Your role always comes from the approver roster (VP Finance if no roster is configured)."""
    return review.roster_role(settings.policy, getattr(args, "reviewer", "")) or "vp_finance"


def _settings(args) -> Settings:
    return load_settings(provider=getattr(args, "provider", None), workers=getattr(args, "workers", None))


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
    before = {r["id"]: r["status"] for r in store.list_invoices()}
    quiet = contextlib.redirect_stdout(io.StringIO()) if args.dry_run else contextlib.nullcontext()
    with InvoicePipeline(settings, store=store, quiet_payments=args.dry_run) as pipeline:
        banner(pipeline, args.dry_run)
        settle_after_crash(store, pipeline)
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


def settle_after_crash(store: Store, pipeline: InvoicePipeline) -> None:
    """Before processing: finish payments a previous crash left unconfirmed, and flag unknown bank outcomes."""
    with contextlib.redirect_stdout(io.StringIO()):
        report = settle_unconfirmed(store, pipeline.backend)
    for r in report:
        colour = "green" if r["outcome"] in ("paid", "already paid - marked paid") else "red"
        console.print(f"[{colour}]Left unsettled by an earlier crash: {r['invoice_number']} (ID {r['invoice_id']}) - {r['outcome']}[/]")


def cmd_review_action(args, settings: Settings) -> int:
    store = Store(settings.db_path)
    store.init()
    with InvoicePipeline(settings, store=store) as pipeline:
        if args.command == "review":
            problem = review.identity_problem(settings.policy, args.reviewer, _role(args, settings))
            if problem:
                console.print(f"[red]{problem}[/] - use --as NAME (or set AP_REVIEWER).")
                return 1
            done = review_cli.interactive(store, settings, pipeline.backend, args.reviewer, _role(args, settings))
            return 0 if done is not None else 1
        return review_cli.act(args.command, store, settings, pipeline.backend, args.invoice_id, args.reviewer, _role(args, settings), args.note)


def main(argv: list[str] | None = None) -> int:
    argv = translate_legacy(list(sys.argv[1:] if argv is None else argv))
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    logging.basicConfig(level=logging.WARNING, format="%(message)s",
                        handlers=[RichHandler(console=console, show_path=False, show_time=False)], force=True)
    for noisy in ("httpx", "httpcore", "openai", "anthropic", "pdfminer", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    settings = _settings(args)
    if args.command == "doctor":
        return doctor.run(settings)
    if args.command == "reset":
        store = Store(settings.db_path)
        store.init()
        return 0 if _confirm_reset(store, args) else 1
    if args.command in ("pending", "show", "payments"):
        store = Store(settings.db_path)
        store.init()
        if args.command == "pending":
            review_cli.pending(store, settings)
            return 0
        if args.command == "payments":
            review_cli.payments(store)
            return 0
        return review_cli.show(store, settings, args.invoice_id, args.reviewer, _role(args, settings))

    dry = getattr(args, "dry_run", False)
    with dry_run_settings(settings) if dry else contextlib.nullcontext(settings) as active:
        if args.command in ("process", "batch"):
            return cmd_process(args, active)
        return cmd_review_action(args, active)


__all__ = ["main", "translate_legacy", "build_parser", "dry_run_settings"]
