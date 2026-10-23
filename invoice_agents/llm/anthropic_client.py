"""Adapter for Anthropic's Messages API (Claude)."""

from __future__ import annotations

import json
from typing import Any

from anthropic import Anthropic

from .base import LLMClient, Tool, ToolCall, ToolStep


class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(self, *, model: str, api_key: str, **kwargs):
        super().__init__(model, **kwargs)
        self._client = Anthropic(api_key=api_key, timeout=self.timeout_s, max_retries=1)

    def _complete_json(self, system, messages, schema, schema_name) -> str:
        # Structured output via a forced tool call: the tool's input *is* the JSON object.
        response = self._client.messages.create(
            model=self.model,
            max_tokens=4096,
            temperature=self.temperature,
            system=system,
            messages=messages,
            tools=[{"name": schema_name, "description": f"Return the {schema_name}.", "input_schema": schema}],
            tool_choice={"type": "tool", "name": schema_name},
        )
        for block in response.content:
            if block.type == "tool_use":
                return json.dumps(block.input)
        return "".join(getattr(b, "text", "") for b in response.content)

    def _tool_step(self, system, history, tools) -> ToolStep:
        kwargs: dict[str, Any] = {}
        if tools:
            kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
        response = self._client.messages.create(
            model=self.model, max_tokens=2048, temperature=self.temperature, system=system, messages=history, **kwargs
        )
        calls = [ToolCall(id=b.id, name=b.name, arguments=dict(b.input)) for b in response.content if b.type == "tool_use"]
        text = "".join(b.text for b in response.content if b.type == "text")
        return ToolStep(text=text, tool_calls=calls, raw_assistant_message=response.content)

    def _append_tool_results(self, history, step, results) -> None:
        history.append({"role": "assistant", "content": [b.model_dump() for b in step.raw_assistant_message]})
        history.append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": call.id, "content": content} for call, content in results],
            }
        )

    def _user_message(self, content: str) -> Any:
        return {"role": "user", "content": content}
