"""Runtime configuration, loaded from environment variables (and an optional .env file).

Everything that differs between environments lives here: which LLM to use, where the
database is, and the business-policy thresholds the approval agent enforces.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass(frozen=True)
class PolicySettings:
    """Approval policy. Amounts are in USD."""

    vp_review_threshold: float = 10_000.0  # invoices at or above this get VP-level scrutiny
    cfo_threshold: float = 50_000.0  # at or above this, never auto-approve; human sign-off
    price_variance_tolerance: float = 0.10  # unit price more than 10% above catalog is flagged
    base_currency: str = "USD"


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
    bank_single_payment_limit: float = 50_000.0  # enforced by the bank (MCP server), not by the agents


def load_settings(**overrides) -> Settings:
    """Build settings from the environment. Keyword overrides (e.g. provider="offline") win."""
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
        vp_review_threshold=float(_env("VP_REVIEW_THRESHOLD", "10000")),
        cfo_threshold=float(_env("CFO_THRESHOLD", "50000")),
        price_variance_tolerance=float(_env("PRICE_VARIANCE_TOLERANCE", "0.10")),
    )
    secondary = None
    if _env("LLM_SECONDARY_PROVIDER"):
        secondary = LLMSettings(
            provider=_env("LLM_SECONDARY_PROVIDER").lower(), model=_env("LLM_SECONDARY_MODEL"),
            ollama_base_url=llm.ollama_base_url, xai_api_key=llm.xai_api_key, openai_api_key=llm.openai_api_key,
            anthropic_api_key=llm.anthropic_api_key, timeout_s=llm.timeout_s, fallback_to_offline=False,
        )
    db_path = overrides.pop("db_path", None) or Path(_env("INVENTORY_DB", str(PROJECT_ROOT / "inventory.db")))
    defaults = {
        "fast_path": _env("FAST_PATH", "true").lower() == "true",
        "workers": int(_env("WORKERS", "4")),
        "tools_backend": _env("TOOLS_BACKEND", "local").lower(),
        "secondary_llm": secondary,
        "bank_single_payment_limit": float(_env("BANK_SINGLE_PAYMENT_LIMIT", "50000")),
    }
    defaults.update({k: v for k, v in overrides.items() if v is not None})
    return Settings(llm=llm, policy=policy, db_path=Path(db_path), **defaults)
