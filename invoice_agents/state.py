"""Shared LangGraph state and the per-run context handed to every agent."""

from __future__ import annotations

import operator
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Annotated, Any, TypedDict

from .config import Settings
from .db import Store
from .llm import LLMClient


class InvoiceState(TypedDict, total=False):
    run_id: str
    invoice_path: str
    # ingestion
    source_format: str
    raw_text: str
    invoice: dict
    extraction_method: str  # parser | llm | heuristic
    extraction_attempts: int
    extraction_feedback: list[str]
    # validation
    validation: dict
    risk: dict
    policy: dict
    # approval
    review_round: int
    draft_decision: dict
    critiques: Annotated[list[dict], operator.add]
    decision: dict
    # payment / outcome
    status: str  # paid | rejected | pending_review | failed
    payment: dict
    invoice_id: int
    error: str
    trace: Annotated[list[dict], operator.add]


@dataclass
class AgentContext:
    settings: Settings
    store: Store
    llm: LLMClient
    today: date = field(default_factory=date.today)


def trace_event(stage: str, event: str, started: float | None = None, **detail: Any) -> dict:
    """One entry in the run's structured trace (rendered by the CLI/UI and written to the audit log)."""
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "stage": stage, "event": event, "detail": detail}
    if started is not None:
        entry["ms"] = round((time.perf_counter() - started) * 1000)
    return entry
