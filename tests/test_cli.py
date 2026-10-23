"""The CLI: subcommands, the brief's legacy flags, interactive review, dry run, doctor, policy."""

import contextlib
import io
from collections import Counter

import pytest
import yaml

from cli import app, doctor, review_cli
from invoice_agents import review
from invoice_agents.config import DEFAULT_POLICY_FILE, load_settings
from invoice_agents.db import Store

from .conftest import DATA


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "cli.db"
    monkeypatch.setenv("INVENTORY_DB", str(db))
    monkeypatch.setenv("LLM_PROVIDER", "offline")
    monkeypatch.delenv("AP_REVIEWER", raising=False)
    return Store(db)


def run(*argv) -> int:
    with contextlib.redirect_stdout(io.StringIO()):
        return app.main(list(argv))


@pytest.mark.parametrize("old,new", [
    (["--invoice_path=data/x.txt"], ["process", "data/x.txt"]),
    (["--invoice_path", "data/x.txt", "--dry-run"], ["process", "data/x.txt", "--dry-run"]),
    (["--invoice-path=data/x.txt"], ["process", "data/x.txt"]),
    (["process", "x"], ["process", "x"]),
    (["batch"], ["batch"]),
])
def test_the_briefs_invoice_path_form_still_works(old, new):
    assert app.translate_legacy(old) == new


def test_removed_options_are_really_gone(env):
    for argv in (["batch", "--fresh"], ["approve", "1", "--role", "cfo"], ["batch", "--model", "x"], ["--all"],
                 ["queue", "status"], ["watch", "x"], ["hold", "1"], ["reopen", "1"], ["reconcile"], ["policy"],
                 ["show", "1", "--json"], ["payments", "--csv", "x"], ["doctor", "--deep"], ["review", "--rejected"]):
        with pytest.raises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                app.main(argv)


def test_no_command_prints_help(env):
    assert run() == 2


def test_brief_command_still_works(env):
    assert run(f"--invoice_path={DATA / 'invoice_1001.txt'}") == 0
    assert env.list_invoices()[0]["status"] == "paid"


def test_batch_and_reset(env):
    assert run("batch", str(DATA), "-q") == 0
    assert Counter(r["status"] for r in env.list_invoices())["paid"] == 6
    assert run("reset", "--yes") == 0
    assert env.list_invoices() == []
    assert run("batch", str(DATA), "--workers", "4", "-q") == 0
    assert Counter(r["status"] for r in env.list_invoices())["paid"] == 6


def test_dry_run_changes_nothing(env):
    run("batch", str(DATA), "-q")
    before = (len(env.list_invoices()), len(env.list_payments()))
    assert run("batch", str(DATA), "--dry-run", "-q") == 0
    assert (len(env.list_invoices()), len(env.list_payments())) == before


def test_review_actions_and_identity_from_env(env, monkeypatch):
    run("batch", str(DATA), "-q")
    fx = next(r for r in env.list_invoices() if r["currency"] == "EUR")
    assert run("reject", str(fx["id"]), "--as", "Dana Lee") == 1  # a rejection needs a reason
    monkeypatch.setenv("AP_REVIEWER", "Dana Lee:vp_finance")
    assert run("approve", str(fx["id"]), "--note", "FX ok") == 0
    assert env.list_approvals(fx["id"])[0]["reviewer"] == "Dana Lee"
    assert run("show", str(fx["id"])) == 0 and run("show", "99999") == 1


def scripted(*answers):
    queue = list(answers)
    return lambda *args, **kwargs: queue.pop(0)


def test_interactive_review_walks_the_queue(env):
    run("batch", str(DATA), "-q")
    settings = load_settings()
    from invoice_agents.backends import LocalBackend

    backend = LocalBackend(settings, env)
    with contextlib.redirect_stdout(io.StringIO()):
        done = review_cli.interactive(env, settings, backend, "Dana Lee", "vp_finance",
                                      ask=scripted("d", "a", "PO amendment confirmed", "h", "", "h", "checking entity", "r", "wrong currency"))
    # 1st: details then approve; 2nd: hold with empty reason (cancelled) then hold properly; 3rd: reject
    assert done == Counter({"paid": 1, "on_hold": 1, "rejected": 1})
    statuses = Counter(r["status"] for r in env.list_invoices())
    assert statuses["on_hold"] == 1 and statuses["pending_review"] == 0


def test_doctor_passes_offline_and_catches_a_policy_typo(env, tmp_path, monkeypatch):
    with contextlib.redirect_stdout(io.StringIO()):
        assert doctor.run(load_settings()) == 0
    data = yaml.safe_load(DEFAULT_POLICY_FILE.read_text(encoding="utf-8"))
    data["always_escalate"]["BANK_DETAILS_CHANGD"] = "typo"
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("AP_POLICY_FILE", str(bad))
    with contextlib.redirect_stdout(io.StringIO()):
        assert doctor.run(load_settings()) == 1


def test_doctor_reports_missing_api_key(env, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    checks = doctor.check_llm(load_settings(provider="xai"), roundtrip=False)
    assert checks[0].status == doctor.FAIL and "XAI_API_KEY" in checks[0].fix


def test_payments_register(env):
    run("batch", str(DATA), "-q")
    assert run("payments") == 0 and run("pending") == 0


def test_commands_accept_invoice_numbers_as_well_as_ids(env):
    run("batch", str(DATA), "-q")
    paid = next(r for r in env.list_invoices() if r["invoice_number"] == "INV-1006")
    for ref in ("INV-1006", "inv 1006", "1006", str(paid["id"])):
        assert review_cli.resolve_ref(env, ref) == paid["id"], ref
    assert run("show", "INV-1006") == 0 and run("show", "INV-9999") == 1


def test_ambiguous_references_are_never_guessed(env):
    run("batch", str(DATA), "-q")
    # Viewing INV-1011 (paid PDF + rejected duplicate TXT) shows the paid one...
    paid = next(r for r in env.list_invoices() if r["invoice_number"] == "INV-1011" and r["status"] == "paid")
    with contextlib.redirect_stdout(io.StringIO()):
        assert review_cli.resolve_ref(env, "INV-1011") == paid["id"]
    # ...but a true tie (INV-1013 rejected as JSON and as PDF) is never guessed
    with pytest.raises(LookupError, match="more than one"):
        review_cli.resolve_ref(env, "INV-1013")
    # ...but an action picks the only entry it can apply to
    fx = next(r for r in env.list_invoices() if r["invoice_number"] == "INV-1014")
    assert review_cli.resolve_ref(env, "INV-1014", review.REVIEWABLE) == fx["id"]
    assert run("approve", "INV-1014", "--as", "Dana Lee", "--note", "FX ok") == 0
    assert env.get_invoice(fx["id"])["status"] == "paid"


def test_bare_number_that_is_both_an_id_and_an_invoice_number_is_refused(env, tmp_path):
    import json

    run("batch", str(DATA), "-q")
    target = env.list_invoices()[-1]["id"]  # some ledger ID; give another invoice that number
    (tmp_path / "x.json").write_text(json.dumps({"invoice_number": str(target), "vendor": {"name": "Widgets Inc."},
                                                 "date": "2026-10-01", "due_date": "2026-10-31", "total": 250.0,
                                                 "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 250.0}]}))
    run("process", str(tmp_path / "x.json"))
    with pytest.raises(LookupError, match="both ledger ID"):
        review_cli.resolve_ref(env, str(target))


def _cli_surface():
    """Every (command, option) the CLI defines, read from the parser itself."""
    import argparse

    def subparsers(p):
        return [a for a in p._actions if isinstance(a, argparse._SubParsersAction)]

    commands, options = set(), set()
    for name, p in subparsers(app.build_parser())[0].choices.items():
        nested = subparsers(p)
        children = [(f"{name} {child}", cp) for n in nested for child, cp in n.choices.items()] or [(name, p)]
        for full, cp in children:
            commands.add(full)
            options |= {o for a in cp._actions for o in a.option_strings if o.startswith("--") and o != "--help"}
    return commands, options


def test_docs_cli_md_documents_every_command_and_option():
    from pathlib import Path
    import re

    doc = (Path(__file__).resolve().parent.parent / "docs" / "CLI.md").read_text(encoding="utf-8")
    commands, options = _cli_surface()
    assert not [c for c in commands if f"main.py {c}" not in doc], "commands missing from docs/CLI.md"
    assert not [o for o in options if o not in doc], "options missing from docs/CLI.md"
    # options of the separate tools (python -m invoice_agents.db / loadtest / synthetic) are not main.py options
    documented = set(re.findall(r"(?<![\w-])(--[a-z][a-z_-]+)", doc)) - {"--invoice_path", "--n", "--out", "--help", "--reset"}
    assert not documented - options, f"docs/CLI.md mentions options that don't exist: {documented - options}"


def test_readme_commands_are_real_commands():
    from pathlib import Path
    import re

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    commands, options = _cli_surface()
    used = set(re.findall(r"python main\.py (\w+)", readme))
    assert used <= {c.split()[0] for c in commands}, used - {c.split()[0] for c in commands}
    flags = set(re.findall(r"python main\.py [^\n`]*?(--[a-z][a-z_-]+)", readme)) - {"--invoice_path"}
    assert flags <= options, flags - options
