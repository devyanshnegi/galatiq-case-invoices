"""LangGraph wiring for the four-stage workflow, and the `InvoicePipeline` entry point used by CLI and UI.

    load ─┬─(structured)──────────────┐
          ├─(free text)──> extract ──> self_check ──(problems & retries left)──> extract
          └─(unreadable)─> failed       │
                                        └──> validate ─┬─(one allowed outcome)─> policy_decision ──────────────┐
                                                       └─(discretion)─> review <──(audit disagrees)── critique  │
                                                                          └──────> critique ──> guardrail <──────┘ ──> payment
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from langgraph.graph import END, START, StateGraph

from .agents.approval import critic_node, guardrail_node, needs_deliberation, policy_decision_node, review_node, should_revise
from .agents.ingestion import extract_node, load_node, qa_node, should_retry_extraction
from .agents.payment import failure_node, payment_node
from .agents.validation import validate_node
from .config import Settings, load_settings
from .db import Store
from .backends import create_backend
from .llm import create_llm
from .llm.base import RUN_CALLS
from .state import AgentContext, InvoiceState

log = logging.getLogger(__name__)

NODE_LABELS = {
    "load": "Ingestion · load document",
    "extract": "Ingestion · extract fields",
    "self_check": "Ingestion · self-check",
    "validate": "Validation · inventory, maths, fraud",
    "policy_decision": "Approval · fast path (policy-determined)",
    "review": "Approval · VP review",
    "critique": "Approval · internal-audit critique",
    "guardrail": "Approval · policy guardrail",
    "payment": "Payment",
    "failed": "Failed",
}


def build_graph(ctx: AgentContext):
    g = StateGraph(InvoiceState)
    g.add_node("load", load_node(ctx))
    g.add_node("extract", extract_node(ctx))
    g.add_node("self_check", qa_node(ctx))
    g.add_node("validate", validate_node(ctx))
    g.add_node("policy_decision", policy_decision_node(ctx))
    g.add_node("review", review_node(ctx))
    g.add_node("critique", critic_node(ctx))
    g.add_node("guardrail", guardrail_node(ctx))
    g.add_node("payment", payment_node(ctx))
    g.add_node("failed", failure_node(ctx))

    g.add_edge(START, "load")
    g.add_conditional_edges(
        "load",
        lambda s: "failed" if s.get("error") else ("self_check" if s.get("extraction_method") == "parser" else "extract"),
        {"failed": "failed", "self_check": "self_check", "extract": "extract"},
    )
    g.add_edge("extract", "self_check")
    g.add_conditional_edges("self_check", should_retry_extraction(ctx), {"retry": "extract", "continue": "validate", "failed": "failed"})
    g.add_conditional_edges("validate", needs_deliberation(ctx), {"fast_path": "policy_decision", "deliberate": "review"})
    g.add_edge("policy_decision", "guardrail")
    g.add_edge("review", "critique")
    g.add_conditional_edges("critique", should_revise(ctx), {"revise": "review", "finalize": "guardrail"})
    g.add_edge("guardrail", "payment")
    g.add_edge("payment", END)
    g.add_edge("failed", END)
    return g.compile()


@dataclass
class BatchResult:
    results: list[tuple[Path, InvoiceState]]
    seconds: float
    workers: int

    @property
    def throughput_per_min(self) -> float:
        return 60 * len(self.results) / self.seconds if self.seconds else 0.0


class InvoicePipeline:
    def __init__(self, settings: Settings | None = None, store: Store | None = None, llm=None, backend=None, quiet_payments: bool = False):
        self.settings = settings or load_settings()
        self.store = store or Store(self.settings.db_path)
        self.store.init()
        self.llm = llm or create_llm(self.settings.llm)
        secondary = create_llm(self.settings.secondary_llm) if self.settings.secondary_llm else None
        self.backend = backend or create_backend(self.settings, self.store, quiet=quiet_payments)
        self.ctx = AgentContext(settings=self.settings, store=self.store, llm=self.llm, backend=self.backend, secondary_llm=secondary)
        self.graph = build_graph(self.ctx)
        self.settings.log_dir.mkdir(parents=True, exist_ok=True)
        self._log_lock = threading.Lock()

    def close(self) -> None:
        self.backend.close()
        self.store.close()

    def __enter__(self) -> "InvoicePipeline":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def stream(self, invoice_path: str | Path, run_id: str | None = None) -> Iterator[tuple[str, dict, InvoiceState]]:
        """Yield (node_name, node_update, accumulated_state) as each agent finishes - drives live UIs."""
        run_id = run_id or uuid.uuid4().hex[:12]
        state: InvoiceState = {"run_id": run_id, "invoice_path": str(invoice_path), "trace": [], "critiques": []}
        calls = [0]
        token = RUN_CALLS.set(calls)
        started = time.perf_counter()
        try:
            for chunk in self.graph.stream(dict(state), stream_mode="updates", config={"recursion_limit": 40}):
                for node, update in chunk.items():
                    update = update or {}
                    for key, value in update.items():
                        if key in ("trace", "critiques"):
                            state[key] = state.get(key, []) + value
                        else:
                            state[key] = value
                    state["llm_calls"] = calls[0]
                    yield node, update, state
        finally:
            RUN_CALLS.reset(token)
        state["llm_calls"] = calls[0]
        duration_ms = round((time.perf_counter() - started) * 1000)
        if state.get("invoice_id"):
            self.store.set_run_metrics(state["invoice_id"], llm_calls=calls[0], duration_ms=duration_ms)
        self._persist_trace(state, duration_ms)

    def process(self, invoice_path: str | Path, on_step: Callable[[str, dict, InvoiceState], None] | None = None,
                run_id: str | None = None) -> InvoiceState:
        state: InvoiceState = {}
        for node, update, state in self.stream(invoice_path, run_id=run_id):
            if on_step:
                on_step(node, update, state)
        return state

    def process_many(self, paths: list[Path], workers: int | None = None,
                     on_done: Callable[[Path, InvoiceState], None] | None = None) -> BatchResult:
        """Process invoices concurrently. Results come back in input order."""
        workers = max(1, workers or self.settings.workers)
        started = time.perf_counter()
        results: dict[int, InvoiceState] = {}

        def one(i: int, path: Path) -> None:
            try:
                state = self.process(path)
            except Exception as exc:  # one bad invoice must never take down the batch
                log.exception("unexpected failure on %s", path)
                state = {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "invoice_path": str(path)}
            results[i] = state
            if on_done:
                on_done(path, state)

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="invoice") as pool:
            list(pool.map(lambda args: one(*args), enumerate(paths)))
        return BatchResult([(p, results[i]) for i, p in enumerate(paths)], time.perf_counter() - started, workers)

    def _persist_trace(self, state: InvoiceState, duration_ms: int) -> None:
        number = (state.get("invoice") or {}).get("invoice_number")
        self.store.audit_many([
            dict(run_id=state["run_id"], invoice_number=number, stage=e["stage"], event=e["event"], detail={**e["detail"], "ms": e.get("ms")})
            for e in state.get("trace", [])
        ])
        record = {
            "run_id": state["run_id"],
            "invoice_path": state.get("invoice_path"),
            "invoice_number": number,
            "status": state.get("status"),
            "decision": (state.get("decision") or {}).get("decision"),
            "llm": self.llm.label,
            "llm_calls": state.get("llm_calls"),
            "duration_ms": duration_ms,
            "trace": state.get("trace"),
        }
        with self._log_lock, open(self.settings.log_dir / "runs.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
