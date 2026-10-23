"""config/ap_policy.yaml drives agent behaviour: editing the policy changes outcomes without code changes."""

import contextlib
import io
import re
from pathlib import Path

import yaml

from invoice_agents.config import DEFAULT_POLICY_FILE, load_settings
from invoice_agents.graph import InvoicePipeline

from .conftest import DATA

ROOT = Path(__file__).resolve().parent.parent


def policy_with(tmp_path, **edits):
    """Copy the real policy file and apply edits like {"approval.vp_review_threshold": 5000}."""
    data = yaml.safe_load(DEFAULT_POLICY_FILE.read_text(encoding="utf-8"))
    for dotted, value in edits.items():
        node = data
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node.setdefault(key, {})
        if value is None:
            node.pop(leaf, None)
        else:
            node[leaf] = value
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def run(tmp_path, policy_path, name):
    settings = load_settings(provider="offline", db_path=tmp_path / "p.db", log_dir=tmp_path / "logs", policy_file=policy_path)
    with contextlib.redirect_stdout(io.StringIO()), InvoicePipeline(settings) as pipeline:
        return pipeline.process(DATA / name)


def test_repo_policy_file_loads_with_expected_values():
    s = load_settings(provider="offline")
    assert (s.policy.policy_id, s.policy.version) == ("FIN-AP-001", "1.0")
    assert (s.policy.vp_review_threshold, s.policy.cfo_threshold, s.bank_single_payment_limit) == (10_000, 50_000, 50_000)
    assert "BANK_DETAILS_CHANGED" in s.policy.always_escalate


def test_every_always_escalate_code_is_a_real_check_code():
    """Catches typos: a misspelled code in the YAML would silently never escalate anything."""
    source = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "invoice_agents").rglob("*.py"))
    emitted = set(re.findall(r'code="([A-Z_]+)"', source))
    unknown = set(load_settings(provider="offline").policy.always_escalate) - emitted
    assert not unknown, f"policy references codes no check emits: {unknown}"


def test_lowering_the_vp_threshold_changes_the_tier(tmp_path):
    state = run(tmp_path, policy_with(tmp_path, **{"approval.vp_review_threshold": 4000}), "invoice_1001.txt")
    assert state["policy"]["tier"] == "vp" and state["policy"]["version"] == "1.0"


def test_adding_an_always_escalate_rule_holds_the_invoice(tmp_path):
    escalate = yaml.safe_load(DEFAULT_POLICY_FILE.read_text())["always_escalate"] | {"PRICE_ABOVE_CATALOG": "every price premium needs a buyer"}
    state = run(tmp_path, policy_with(tmp_path, always_escalate=escalate), "invoice_1010.txt")
    assert state["status"] == "pending_review"  # paid under the default policy


def test_removing_a_currency_rejects_it(tmp_path):
    state = run(tmp_path, policy_with(tmp_path, **{"currency.reference_rates_to_usd": {"USD": 1.0}}), "invoice_1014.xml")
    assert state["status"] == "rejected"
    assert "UNSUPPORTED_CURRENCY" in {i["code"] for i in state["validation"]["issues"]}


def test_bank_limit_comes_from_policy(tmp_path):
    state = run(tmp_path, policy_with(tmp_path, **{"payments.single_payment_limit": 1000}), "invoice_1001.txt")
    assert state["status"] == "pending_review" and state["payment"]["status"] == "declined"


def test_environment_overrides_policy_file(tmp_path, monkeypatch):
    monkeypatch.setenv("VP_REVIEW_THRESHOLD", "123")
    assert load_settings(provider="offline", policy_file=policy_with(tmp_path)).policy.vp_review_threshold == 123


def test_missing_policy_file_falls_back_to_builtin_defaults(tmp_path):
    s = load_settings(provider="offline", policy_file=tmp_path / "nope.yaml")
    assert s.policy.version == "builtin" and s.policy.vp_review_threshold == 10_000


def test_reviewer_sees_policy_version():
    from invoice_agents.policy import policy_text

    assert "FIN-AP-001 v1.0" in policy_text(load_settings(provider="offline").policy)
