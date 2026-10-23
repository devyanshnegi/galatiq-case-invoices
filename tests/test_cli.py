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
    (["--invoice_path", "data/x.txt", "--json"], ["process", "data/x.txt", "--json"]),
    (["--all"], ["batch"]),
    (["--all", "d", "--workers", "8"], ["batch", "d", "--workers", "8"]),
    (["--queue", "inbox", "--reset"], ["queue", "run", "inbox", "--fresh"]),
    (["--queue-status"], ["queue", "status"]),
    (["--pending"], ["pending"]),
    (["--approve", "7", "--reviewer", "Ann", "--note", "ok"], ["approve", "7", "--as", "Ann", "--note", "ok"]),
    (["process", "x"], ["process", "x"]),
])
def test_legacy_flags_translate(old, new):
    assert app.translate_legacy(old) == new


def test_no_command_prints_help(env):
    assert run() == 2


def test_brief_command_still_works(env):
    assert run(f"--invoice_path={DATA / 'invoice_1001.txt'}") == 0
    assert env.list_invoices()[0]["status"] == "paid"


def test_batch_and_queue_subcommands(env):
    assert run("batch", str(DATA), "-q") == 0
    assert Counter(r["status"] for r in env.list_invoices())["paid"] == 6
    assert run("queue", "run", str(DATA), "--fresh", "-y", "-q") == 0
    assert run("queue", "status") == 0


def test_dry_run_changes_nothing(env):
    run("batch", str(DATA), "-q")
    before = (len(env.list_invoices()), len(env.list_payments()))
    assert run("batch", str(DATA), "--fresh", "-y", "--dry-run", "-q") == 0  # even a fresh reprocess
    pending = [r for r in env.list_invoices() if r["status"] == "pending_review"]
    assert run("approve", str(pending[0]["id"]), "--as", "Dana", "--dry-run") == 0
    assert (len(env.list_invoices()), len(env.list_payments())) == before
    assert env.list_approvals(pending[0]["id"]) == [] and env.get_invoice(pending[0]["id"])["status"] == "pending_review"


def test_review_actions_and_identity_from_env(env, monkeypatch):
    run("batch", str(DATA), "-q")
    fx = next(r for r in env.list_invoices() if r["currency"] == "EUR")
    assert run("hold", str(fx["id"])) == 1  # hold needs a note
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


def test_interactive_rejected_mode_reopens(env):
    run("batch", str(DATA), "-q")
    settings = load_settings()
    from invoice_agents.backends import LocalBackend

    with contextlib.redirect_stdout(io.StringIO()):
        done = review_cli.interactive(env, settings, LocalBackend(settings, env), "Dana", "vp_finance", rejected=True,
                                      ask=scripted("q"))
        assert done == Counter()
        # first rejected invoice in id order that can be reopened -> reopen it, quit after
        cases = review.list_cases(env, settings.policy, statuses=("rejected",))
        first_reopenable = next(i for i, c in enumerate(cases) if review.why_cannot_reopen(c, settings.policy) is None)
        answers = ["s"] * first_reopenable + ["o", "Stock confirmed delivered from a second warehouse", "q"]
        done = review_cli.interactive(env, settings, LocalBackend(settings, env), "Dana", "vp_finance", rejected=True, ask=scripted(*answers))
    assert done["pending_review"] == 1


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
    assert run("policy") == 1


def test_doctor_reports_missing_api_key(env, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    checks = doctor.check_llm(load_settings(provider="xai"), deep=False)
    assert checks[0].status == doctor.FAIL and "XAI_API_KEY" in checks[0].fix


def test_payments_csv(env, tmp_path):
    run("batch", str(DATA), "-q")
    out = tmp_path / "register.csv"
    assert run("payments", "--csv", str(out)) == 0
    assert len(out.read_text(encoding="utf-8").strip().splitlines()) == 1 + 6
