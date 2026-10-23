"""LangGraph wiring for the four-stage workflow, and the `InvoicePipeline` entry point used by CLI and UI.

    load ─┬─(structured)──────────────┐
          ├─(free text)──> extract ──> self_check ──(problems & retries left)──> extract
          └─(unreadable)─> failed       │
                                        └──> validate ──> review <──(audit disagrees)── critique
                                                            └──────────> critique ──> guardrail ──> payment
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Callable, Iterator

from langgraph.graph import END, START, StateGraph

from .agents.approval import critic_node, guardrail_node, review_node, should_revise
from .agents.ingestion import extract_node, load_node, qa_node, should_retry_extraction
from .agents.payment import failure_node, payment_node
from .agents.validation import validate_node
from .config import Settings, load_settings
from .db import Store
from .llm import create_llm
from .state import AgentContext, InvoiceState

log = logging.getLogger(__name__)

NODE_LABELS = {
    "load": "Ingestion · load document",
    "extract": "Ingestion · extract fields",
    "self_check": "Ingestion · self-check",
    "validate": "Validation · inventory, maths, fraud",
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
    g.add_edge("validate", "review")
    g.add_edge("review", "critique")
    g.add_conditional_edges("critique", should_revise(ctx), {"revise": "review", "finalize": "guardrail"})
    g.add_edge("guardrail", "payment")
    g.add_edge("payment", END)
    g.add_edge("failed", END)
    return g.compile()


class InvoicePipeline:
    def __init__(self, settings: Settings | None = None, store: Store | None = None, llm=None):
        self.settings = settings or load_settings()
        self.store = store or Store(self.settings.db_path)
        self.store.init()
        self.llm = llm or create_llm(self.settings.llm)
        self.ctx = AgentContext(settings=self.settings, store=self.store, llm=self.llm)
        self.graph = build_graph(self.ctx)
        self.settings.log_dir.mkdir(parents=True, exist_ok=True)

    def stream(self, invoice_path: str | Path) -> Iterator[tuple[str, dict, InvoiceState]]:
        """Yield (node_name, node_update, accumulated_state) as each agent finishes - drives live UIs."""
        run_id = uuid.uuid4().hex[:12]
        state: InvoiceState = {"run_id": run_id, "invoice_path": str(invoice_path), "trace": [], "critiques": []}
        for chunk in self.graph.stream(dict(state), stream_mode="updates", config={"recursion_limit": 40}):
            for node, update in chunk.items():
                update = update or {}
                for key, value in update.items():
                    if key in ("trace", "critiques"):
                        state[key] = state.get(key, []) + value
                    else:
                        state[key] = value
                yield node, update, state
        self._persist_trace(state)

    def process(self, invoice_path: str | Path, on_step: Callable[[str, dict, InvoiceState], None] | None = None) -> InvoiceState:
        state: InvoiceState = {}
        for node, update, state in self.stream(invoice_path):
            if on_step:
                on_step(node, update, state)
        return state

    def _persist_trace(self, state: InvoiceState) -> None:
        number = (state.get("invoice") or {}).get("invoice_number")
        for event in state.get("trace", []):
            self.store.audit(run_id=state["run_id"], invoice_number=number, stage=event["stage"], event=event["event"],
                             detail={**event["detail"], "ms": event.get("ms")})
        record = {
            "run_id": state["run_id"],
            "invoice_path": state.get("invoice_path"),
            "invoice_number": number,
            "status": state.get("status"),
            "decision": (state.get("decision") or {}).get("decision"),
            "llm": self.llm.label,
            "llm_calls": self.llm.usage.calls,
            "trace": state.get("trace"),
        }
        with open(self.settings.log_dir / "runs.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
