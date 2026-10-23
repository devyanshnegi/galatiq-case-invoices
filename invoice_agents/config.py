"""Runtime configuration.

Two sources, in increasing precedence:
  1. config/ap_policy.yaml - Acme's AP policy values (thresholds, always-escalate list, limits).
     The human-readable policy is docs/AP_POLICY.md. Override the path with AP_POLICY_FILE.
  2. Environment variables / .env - which LLM, where the database is, and ad-hoc policy overrides.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class LLMSettings:
    provider: str = "ollama"  # ollama | xai | openai | anthropic | offline
    model: str | None = None  # None -> provider default
    temperature: float = 0.0
    timeout_s: float = 120.0
    ollama_base_url: str = "http://localhost:11434/v1"
    xai_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    # When the configured model is unreachable, fall back to deterministic logic instead of crashing.
    fallback_to_offline: bool = True


DEFAULT_POLICY_FILE = PROJECT_ROOT / "config" / "ap_policy.yaml"

# Used only if no policy file is present.
DEFAULT_ALWAYS_ESCALATE = {
    "FOREIGN_CURRENCY": "foreign-currency invoices need treasury to confirm the FX rate",
    "REVISED_INVOICE": "a revision of an invoice we already acted on needs AP to reconcile against the PO",
    "INVOICE_NUMBER_COLLISION": "an invoice number reused by a different vendor needs investigation",
    "VENDOR_ADDRESS_MISMATCH": "a changed remit address is a classic payment-redirection fraud pattern",
    "VENDOR_NAME_VARIANT": "a look-alike vendor name must be confirmed as the same legal entity",
    "UNKNOWN_VENDOR": "first payment to a vendor not in the vendor master needs onboarding/KYC",
    "BANK_DETAILS_CHANGED": "a request to pay new bank details must be verified out-of-band before any payment",
    "THRESHOLD_SPLITTING": "near-threshold invoices from one vendor within days may be split to dodge VP review",
    "EXTRACTION_UNVERIFIED": "the extracted data could not be verified against the document - a human must check it",
}
DEFAULT_FX = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "CAD": 0.73}
DEFAULT_APPROVER_LIMITS = {"ap_specialist": 10_000.0, "department_approver": 10_000.0, "vp_finance": 50_000.0, "cfo": None}


@dataclass(frozen=True)
class PolicySettings:
    """Approval policy (docs/AP_POLICY.md). Amounts are in the base currency."""

    vp_review_threshold: float = 10_000.0  # invoices at or above this get VP-level scrutiny
    cfo_threshold: float = 50_000.0  # at or above this, never auto-approve; human sign-off
    price_variance_tolerance: float = 0.10  # unit price more than 10% above catalog is flagged
    base_currency: str = "USD"
    fx_rates: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_FX))
    terms_tolerance_days: int = 3
    split_window_days: int = 3
    split_min_share: float = 0.5
    always_escalate: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ALWAYS_ESCALATE))
    precedent_min_approvals: int = 2
    # Human review (§4.3, §15)
    approver_limits: dict[str, float | None] = field(default_factory=lambda: dict(DEFAULT_APPROVER_LIMITS))
    two_person_threshold: float = 50_000.0
    two_person_codes: tuple[str, ...] = ("BANK_DETAILS_CHANGED", "VENDOR_ADDRESS_MISMATCH")
    senior_roles: tuple[str, ...] = ("vp_finance", "cfo")
    override_approvals: int = 2
    override_required_role: str = "cfo"
    not_overridable: tuple[str, ...] = ("DUPLICATE_INVOICE", "PROMPT_INJECTION")
    policy_id: str = "FIN-AP-001"
    version: str = "builtin"


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings = field(default_factory=LLMSettings)
    policy: PolicySettings = field(default_factory=PolicySettings)
    db_path: Path = PROJECT_ROOT / "inventory.db"
    log_dir: Path = PROJECT_ROOT / "logs"
    max_extraction_attempts: int = 3  # initial extraction + up to 2 self-corrections
    max_review_rounds: int = 3  # initial decision + up to 2 critique-driven revisions
    # Skip LLM deliberation when the policy leaves exactly one possible outcome (clean -> approve,
    # blocking error -> reject). Deliberation is spent only where there's discretion.
    fast_path: bool = True
    workers: int = 4  # concurrent invoices in batch / queue mode
    tools_backend: str = "local"  # local | mcp  (ERP + bank tools in-process, or via MCP servers)
    secondary_llm: LLMSettings | None = None  # optional second-opinion model for hard extractions
    policy_file: Path | None = None  # which policy file was loaded (passed on to MCP servers)
    bank_single_payment_limit: float = 50_000.0  # enforced by the bank (MCP server), not by the agents
    bank_dual_approved_limit: float = 250_000.0  # payments two humans approved may go up to this


def load_policy_file(path: Path | None = None) -> dict[str, Any]:
    """Read the YAML policy. Missing file -> {} (built-in defaults apply)."""
    path = Path(path or _env("AP_POLICY_FILE") or DEFAULT_POLICY_FILE)
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: policy file must be a mapping")
    return data


def _get(data: dict, dotted: str, default: Any) -> Any:
    node: Any = data
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node or node[key] is None:
            return default
        node = node[key]
    return node


def _num(env_name: str, data: dict, dotted: str, default: float) -> float:
    """Environment wins (for experiments), then the policy file, then the built-in default."""
    return float(_env(env_name) or _get(data, dotted, default))


def load_settings(**overrides) -> Settings:
    """Build settings from the policy file and environment. Keyword overrides (e.g. provider="offline") win."""
    policy_path = overrides.pop("policy_file", None) or _env("AP_POLICY_FILE") or DEFAULT_POLICY_FILE
    pf = load_policy_file(policy_path)
    llm = LLMSettings(
        provider=(overrides.pop("provider", None) or _env("LLM_PROVIDER", "ollama")).lower(),
        model=overrides.pop("model", None) or _env("LLM_MODEL"),
        temperature=float(_env("LLM_TEMPERATURE", "0")),
        timeout_s=float(_env("LLM_TIMEOUT_S", "120")),
        ollama_base_url=_env("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        xai_api_key=_env("XAI_API_KEY"),
        openai_api_key=_env("OPENAI_API_KEY"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        fallback_to_offline=_env("LLM_FALLBACK_TO_OFFLINE", "true").lower() == "true",
    )
    policy = PolicySettings(
        vp_review_threshold=_num("VP_REVIEW_THRESHOLD", pf, "approval.vp_review_threshold", 10_000),
        cfo_threshold=_num("CFO_THRESHOLD", pf, "approval.cfo_threshold", 50_000),
        price_variance_tolerance=_num("PRICE_VARIANCE_TOLERANCE", pf, "matching.price_variance_tolerance", 0.10),
        base_currency=str(_get(pf, "currency.base", "USD")).upper(),
        fx_rates={k.upper(): float(v) for k, v in _get(pf, "currency.reference_rates_to_usd", DEFAULT_FX).items()},
        terms_tolerance_days=int(_get(pf, "terms.due_date_tolerance_days", 3)),
        split_window_days=int(_get(pf, "anti_splitting.window_days", 3)),
        split_min_share=float(_get(pf, "anti_splitting.min_share_of_vp_threshold", 0.5)),
        always_escalate=dict(_get(pf, "always_escalate", DEFAULT_ALWAYS_ESCALATE)),
        precedent_min_approvals=int(_get(pf, "automation.precedent_min_approvals", 2)),
        approver_limits={k: (None if v is None else float(v))
                         for k, v in (_get(pf, "approval.approver_limits", None) or DEFAULT_APPROVER_LIMITS).items()},
        two_person_threshold=float(_get(pf, "human_review.two_person_approval_at", 50_000)),
        two_person_codes=tuple(_get(pf, "human_review.two_person_for_codes", ["BANK_DETAILS_CHANGED", "VENDOR_ADDRESS_MISMATCH"])),
        senior_roles=tuple(_get(pf, "human_review.senior_roles", ["vp_finance", "cfo"])),
        override_approvals=int(_get(pf, "human_review.override.approvals_required", 2)),
        override_required_role=str(_get(pf, "human_review.override.required_role", "cfo")),
        not_overridable=tuple(_get(pf, "human_review.override.not_overridable", ["DUPLICATE_INVOICE", "PROMPT_INJECTION"])),
        policy_id=str(_get(pf, "policy.id", "FIN-AP-001")),
        version=str(_get(pf, "policy.version", "builtin")),
    )
    secondary = None
    if _env("LLM_SECONDARY_PROVIDER"):
        secondary = LLMSettings(
            provider=_env("LLM_SECONDARY_PROVIDER").lower(), model=_env("LLM_SECONDARY_MODEL"),
            ollama_base_url=llm.ollama_base_url, xai_api_key=llm.xai_api_key, openai_api_key=llm.openai_api_key,
            anthropic_api_key=llm.anthropic_api_key, timeout_s=llm.timeout_s, fallback_to_offline=False,
        )
    db_path = overrides.pop("db_path", None) or Path(_env("INVENTORY_DB", str(PROJECT_ROOT / "inventory.db")))
    fast_path_default = "true" if _get(pf, "automation.fast_path", True) else "false"
    defaults = {
        "fast_path": _env("FAST_PATH", fast_path_default).lower() == "true",
        "workers": int(_env("WORKERS", "4")),
        "tools_backend": _env("TOOLS_BACKEND", "local").lower(),
        "secondary_llm": secondary,
        "policy_file": Path(policy_path) if pf else None,
        "bank_single_payment_limit": _num("BANK_SINGLE_PAYMENT_LIMIT", pf, "payments.single_payment_limit", 50_000),
        "bank_dual_approved_limit": float(_get(pf, "payments.dual_approved_payment_limit", 250_000)),
        "max_extraction_attempts": int(_get(pf, "automation.max_extraction_attempts", 3)),
        "max_review_rounds": int(_get(pf, "automation.max_review_rounds", 3)),
    }
    defaults.update({k: v for k, v in overrides.items() if v is not None})
    return Settings(llm=llm, policy=policy, db_path=Path(db_path), **defaults)
