"""Provider-agnostic LLM interface.

Agents only ever talk to `LLMClient`. Each provider adapter implements two primitives:

* `_complete_json`  - one completion constrained to a JSON schema, using the provider's
                      native structured-output feature.
* `_tool_step`      - one turn of a function-calling conversation.

Everything else (schema validation, re-asking on malformed output, the tool-calling loop)
lives here, so adding a new provider is ~50 lines and behaviour stays identical across them.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from contextvars import ContextVar
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """The model could not be reached or kept returning unusable output."""


@dataclass
class Tool:
    """A function the model may call. `parameters` is a JSON schema for the arguments."""

    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., Any]

    def invoke(self, arguments: dict[str, Any]) -> Any:
        try:
            return self.fn(**arguments)
        except TypeError as exc:  # model passed wrong/missing arguments
            return {"error": f"bad arguments for {self.name}: {exc}"}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolStep:
    """What the model produced in one turn: either tool calls, or a final text answer."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_assistant_message: Any = None  # provider-specific, echoed back into the history


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict[str, Any]
    result: Any


@dataclass
class ToolLoopResult:
    text: str
    calls: list[ToolCallRecord]


@dataclass
class LLMUsage:
    calls: int = 0
    seconds: float = 0.0


# Per-invoice call counter. Set by the pipeline for each run so concurrent invoices sharing one
# client still get their own count (the value is a mutable list so LangGraph's copied contexts share it).
RUN_CALLS: ContextVar[list[int] | None] = ContextVar("RUN_CALLS", default=None)


def inline_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema with all `$ref`s inlined.

    Several local runtimes (and some hosted APIs) handle `$defs`/`$ref` poorly when compiling
    a schema into a decoding grammar, so we hand them a flat, self-contained schema.
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                target = node["$ref"].split("/")[-1]
                return resolve(copy.deepcopy(defs[target]))
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


def _extract_json(text: str) -> Any:
    """Parse JSON even if the model wrapped it in prose or ```json fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


class LLMClient(ABC):
    provider: str = "base"
    is_offline: bool = False

    def __init__(self, model: str, temperature: float = 0.0, timeout_s: float = 120.0):
        self.model = model
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.usage = LLMUsage()
        self._usage_lock = threading.Lock()

    def _record(self, seconds: float) -> None:
        with self._usage_lock:
            self.usage.calls += 1
            self.usage.seconds += seconds
        counter = RUN_CALLS.get()
        if counter is not None:
            counter[0] += 1

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"

    # ---- provider primitives -------------------------------------------------------------

    @abstractmethod
    def _complete_json(self, system: str, messages: list[dict[str, str]], schema: dict[str, Any], schema_name: str) -> str:
        """Return the raw text of a completion constrained to `schema`."""

    @abstractmethod
    def _tool_step(self, system: str, history: list[Any], tools: list[Tool]) -> ToolStep:
        """Run one model turn with `tools` available."""

    @abstractmethod
    def _append_tool_results(self, history: list[Any], step: ToolStep, results: list[tuple[ToolCall, str]]) -> None:
        """Append the assistant turn and the tool results to `history` in the provider's format."""

    @abstractmethod
    def _user_message(self, content: str) -> Any:
        """A user message in the provider's history format."""

    # ---- shared behaviour ----------------------------------------------------------------

    def structured(self, *, system: str, user: str, schema: type[T], max_attempts: int = 2) -> T:
        """Get a response that validates against `schema`, re-asking with the error if it doesn't."""
        messages = [{"role": "user", "content": user}]
        json_schema = inline_schema(schema)
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            started = time.perf_counter()
            try:
                raw = self._complete_json(system, messages, json_schema, schema.__name__)
            except Exception as exc:  # network / auth / server errors
                raise LLMError(f"{self.label} request failed: {exc}") from exc
            finally:
                self._record(time.perf_counter() - started)
            try:
                return schema.model_validate(_extract_json(raw))
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
                log.warning("%s returned invalid %s (attempt %d): %s", self.label, schema.__name__, attempt, exc)
                messages += [
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": f"That output did not match the required schema:\n{exc}\n"
                        "Return corrected JSON only.",
                    },
                ]
        raise LLMError(f"{self.label} could not produce a valid {schema.__name__}: {last_error}")

    def run_tools(self, *, system: str, user: str, tools: list[Tool], max_steps: int = 6) -> ToolLoopResult:
        """Let the model call tools until it gives a final answer (or `max_steps` runs out)."""
        history: list[Any] = [self._user_message(user)]
        records: list[ToolCallRecord] = []
        by_name = {t.name: t for t in tools}
        for _ in range(max_steps):
            started = time.perf_counter()
            try:
                step = self._tool_step(system, history, tools)
            except Exception as exc:
                raise LLMError(f"{self.label} tool call failed: {exc}") from exc
            finally:
                self._record(time.perf_counter() - started)
            if not step.tool_calls:
                return ToolLoopResult(text=step.text, calls=records)
            results = []
            for call in step.tool_calls:
                tool = by_name.get(call.name)
                result = tool.invoke(call.arguments) if tool else {"error": f"unknown tool {call.name}"}
                records.append(ToolCallRecord(call.name, call.arguments, result))
                results.append((call, json.dumps(result, default=str)))
            self._append_tool_results(history, step, results)
        history.append(self._user_message("Stop calling tools now and give your final summary."))
        started = time.perf_counter()
        step = self._tool_step(system, history, [])
        self._record(time.perf_counter() - started)
        return ToolLoopResult(text=step.text, calls=records)
