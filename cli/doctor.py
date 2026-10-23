"""`python main.py doctor`: is everything set up? One screen, with a fix for every problem."""

from __future__ import annotations

import importlib
import json
import sys
import time
import urllib.request
from dataclasses import dataclass

from rich.table import Table

from invoice_agents.config import PROJECT_ROOT, Settings
from invoice_agents.db import Store

from .render import console

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
ICON = {OK: "[green]✓[/]", WARN: "[yellow]![/]", FAIL: "[red]✗[/]", SKIP: "[dim]–[/]"}


@dataclass
class Check:
    status: str
    name: str
    detail: str
    fix: str = ""


def _get_json(url: str, timeout: float = 3) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def check_python() -> list[Check]:
    v = sys.version_info
    ok = v >= (3, 11)
    out = [Check(OK if ok else FAIL, "Python", f"{v.major}.{v.minor}.{v.micro}", "" if ok else "use Python 3.11+")]
    missing = []
    versions = []
    for module, dist in (("langgraph", "langgraph"), ("pydantic", "pydantic"), ("openai", "openai"), ("anthropic", "anthropic"),
                         ("mcp", "mcp"), ("pdfplumber", "pdfplumber"), ("yaml", "pyyaml"), ("streamlit", "streamlit")):
        try:
            importlib.import_module(module)
            from importlib.metadata import version

            versions.append(f"{dist} {version(dist)}")
        except Exception:
            missing.append(dist)
    out.append(Check(FAIL if missing else OK, "Dependencies", ", ".join(versions) if not missing else f"missing: {', '.join(missing)}",
                     "pip install -r requirements.txt" if missing else ""))
    return out


def check_policy(settings: Settings) -> list[Check]:
    from invoice_agents.policy import policy_problems

    p = settings.policy
    roster = f"; {len(p.reviewers)} people on the approver roster" if p.reviewers else "; no approver roster (anyone can claim any role)"
    if not settings.policy_file:
        return [Check(WARN, "Policy", "no policy file found - using built-in defaults", "create config/ap_policy.yaml (see docs/AP_POLICY.md)")]
    problems = policy_problems(settings)
    detail = f"{p.policy_id} v{p.version} from {settings.policy_file.name}"
    if problems:
        return [Check(FAIL, "Policy", f"{detail}: {len(problems)} problem(s) - {problems[0]}", "python main.py policy  (lists them all)")]
    return [Check(OK if p.reviewers else WARN, "Policy", f"{detail}; {len(p.always_escalate)} always-escalate rules{roster}",
                  "" if p.reviewers else "add human_review.reviewers to config/ap_policy.yaml")]


def check_database(settings: Settings) -> list[Check]:
    path = settings.db_path
    try:
        store = Store(path)
        store.init()
        with store.connect() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            items = conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
            vendors = conn.execute("SELECT COUNT(*) FROM vendors").fetchone()[0]
            invoices = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
            pending = conn.execute("SELECT COUNT(*) FROM invoices WHERE status IN ('pending_review','on_hold')").fetchone()[0]
            conn.execute("BEGIN IMMEDIATE")  # proves we can write
            conn.execute("ROLLBACK")
        store.close()
    except Exception as exc:
        return [Check(FAIL, "Database", f"{path}: {exc}", "python -m invoice_agents.db --reset")]
    status = OK if items and mode.lower() == "wal" else WARN
    out = [Check(status, "Database", f"{path.name}: {items} SKUs, {vendors} vendors, {invoices} invoices ({pending} awaiting review), {mode} mode",
                 "" if status == OK else "python -m invoice_agents.db --reset")]
    try:
        store = Store(path)
        found = store.unsettled()
        store.close()
        stuck, unknown = len(found["approved_unpaid"]), len(found["unknown_payments"])
        if stuck or unknown:
            out.append(Check(FAIL if unknown else WARN, "Unsettled payments",
                             f"{stuck} approved but unpaid after a crash, {unknown} with an unknown bank outcome",
                             "python main.py reconcile"))
        else:
            out.append(Check(OK, "Unsettled payments", "none"))
    except Exception as exc:
        out.append(Check(WARN, "Unsettled payments", f"couldn't check: {exc}"))
    return out


def check_samples() -> list[Check]:
    samples = PROJECT_ROOT / "data" / "invoices"
    n = len([p for p in samples.glob("*") if p.suffix in {".txt", ".pdf", ".json", ".csv", ".xml"}]) if samples.exists() else 0
    return [Check(OK if n else WARN, "Sample invoices", f"{n} files in data/invoices", "" if n else "restore data/invoices from the repo")]


def check_llm(settings: Settings, deep: bool, label: str = "LLM", llm_settings=None) -> list[Check]:
    s = llm_settings or settings.llm
    from invoice_agents.llm import DEFAULT_MODELS

    model = s.model or DEFAULT_MODELS.get(s.provider, "?")
    if s.provider == "offline":
        return [Check(OK, label, "offline rule-based agents (no model)", "set LLM_PROVIDER=ollama to use a local model")]
    if s.provider == "ollama":
        base = s.ollama_base_url.rstrip("/").removesuffix("/v1")
        try:
            version = _get_json(f"{base}/api/version").get("version")
        except Exception:
            return [Check(FAIL, label, f"Ollama not reachable at {base} - agents will fall back to offline rules",
                          "start Ollama (ollama serve), or install it: winget install Ollama.Ollama")]
        names = {m["name"] for m in _get_json(f"{base}/api/tags").get("models", [])}
        if model not in names and f"{model}:latest" not in names:
            return [Check(FAIL, label, f"Ollama {version} is running but model '{model}' isn't pulled", f"ollama pull {model}")]
        out = [Check(OK, label, f"Ollama {version}, model {model} available")]
    else:
        key = {"xai": s.xai_api_key, "anthropic": s.anthropic_api_key, "openai": s.openai_api_key}.get(s.provider)
        if not key:
            env = {"xai": "XAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}[s.provider]
            return [Check(FAIL, label, f"{s.provider} selected but {env} is not set - agents will fall back to offline rules",
                          f"add {env}=... to .env")]
        out = [Check(OK, label, f"{s.provider}, model {model}, API key set")]
    if deep:
        out.append(_llm_roundtrip(s, label))
    return out


def _llm_roundtrip(s, label: str) -> Check:
    from dataclasses import replace

    from pydantic import BaseModel

    from invoice_agents.llm import create_llm

    class Ping(BaseModel):
        ok: bool

    try:
        llm = create_llm(replace(s, fallback_to_offline=False, timeout_s=60))
        started = time.perf_counter()
        llm.structured(system="Reply with JSON.", user='Return {"ok": true}.', schema=Ping)
        return Check(OK, f"{label} round-trip", f"structured output works ({time.perf_counter() - started:.1f}s)")
    except Exception as exc:
        return Check(FAIL, f"{label} round-trip", f"{type(exc).__name__}: {str(exc)[:120]}", "check the model name and that it supports JSON output")


def check_mcp(settings: Settings, deep: bool) -> list[Check]:
    if settings.tools_backend != "mcp" and not deep:
        return [Check(SKIP, "MCP servers", "tools run in-process (TOOLS_BACKEND=local)", "add --deep to test the MCP servers anyway")]
    from invoice_agents.backends import MCPBackend

    try:
        started = time.perf_counter()
        backend = MCPBackend(settings, timeout_s=30)
        tools = [t.name for t in backend.reviewer_tools()]
        backend.close()
        return [Check(OK, "MCP servers", f"acme-erp ({len(tools)} tools) and acme-bank started in {time.perf_counter() - started:.1f}s")]
    except Exception as exc:
        return [Check(FAIL, "MCP servers", f"could not start: {str(exc)[:120]}", "pip install -r requirements.txt; run from the project folder")]


def run(settings: Settings, deep: bool = False) -> int:
    checks = [*check_python(), *check_policy(settings), *check_database(settings), *check_samples(), *check_llm(settings, deep)]
    if settings.secondary_llm:
        checks += check_llm(settings, deep, "Second-opinion LLM", settings.secondary_llm)
    checks += check_mcp(settings, deep)

    table = Table("", "Check", "Result", "Fix", header_style="bold", box=None, title="Acme AP doctor", title_justify="left")
    for c in checks:
        table.add_row(ICON[c.status], c.name, c.detail, f"[cyan]{c.fix}[/]" if c.fix and c.status != OK else "")
    console.print(table)
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    if fails:
        console.print(f"[red]{fails} problem(s)[/] to fix" + (f", {warns} warning(s)" if warns else "") + ".")
    else:
        console.print("[green]All good.[/]" + (f" {warns} warning(s)." if warns else "") + ("" if deep else " [dim](--deep also calls the model and starts the MCP servers)[/]"))
    return 1 if fails else 0

