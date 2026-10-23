from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path

import pytest

from invoice_agents.config import load_settings
from invoice_agents.db import Store
from invoice_agents.llm.base import LLMClient, ToolStep

DATA = Path(__file__).resolve().parent.parent / "data" / "invoices"


@pytest.fixture
def settings(tmp_path):
    return load_settings(provider="offline", db_path=tmp_path / "test.db", log_dir=tmp_path / "logs")


@pytest.fixture
def store(settings):
    s = Store(settings.db_path)
    s.init(reset=True)
    return s


class FakeLLM(LLMClient):
    """Scripted model: returns queued responses per schema name, and queued tool steps.

    Lets us test the agentic loops (self-correction, reflection, tool use) deterministically.
    """

    provider = "fake"

    def __init__(self, responses: dict[str, list] | None = None, tool_steps: list[ToolStep] | None = None):
        super().__init__("scripted")
        self.responses = defaultdict(deque, {k: deque(v) for k, v in (responses or {}).items()})
        self.tool_steps = deque(tool_steps or [])
        self.prompts: dict[str, list[str]] = defaultdict(list)

    def _complete_json(self, system, messages, schema, schema_name):
        self.prompts[schema_name].append(messages[-1]["content"])
        queue = self.responses[schema_name]
        if not queue:
            raise RuntimeError(f"no scripted response for {schema_name}")
        item = queue.popleft() if len(queue) > 1 else queue[0]  # last response repeats
        return item if isinstance(item, str) else json.dumps(item)

    def _tool_step(self, system, history, tools):
        return self.tool_steps.popleft() if self.tool_steps else ToolStep(text="Investigation complete.")

    def _append_tool_results(self, history, step, results):
        history.append({"role": "assistant", "content": "(tool calls)"})
        history.extend({"role": "tool", "content": content} for _, content in results)

    def _user_message(self, content):
        return {"role": "user", "content": content}
