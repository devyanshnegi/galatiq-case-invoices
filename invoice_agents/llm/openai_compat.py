"""Adapter for any OpenAI-compatible chat API: Ollama (local), xAI Grok, OpenAI, vLLM, LM Studio..."""

from __future__ import annotations

import json
from typing import Any

from openai import OpenAI

from .base import LLMClient, Tool, ToolCall, ToolStep


class OpenAICompatibleClient(LLMClient):
    def __init__(self, *, provider: str, model: str, base_url: str | None, api_key: str, **kwargs):
        super().__init__(model, **kwargs)
        self.provider = provider
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=self.timeout_s, max_retries=1)

    def _complete_json(self, system, messages, schema, schema_name) -> str:
        response = self._client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=[{"role": "system", "content": system}, *messages],
            # Native constrained decoding: Ollama compiles this into a grammar, xAI/OpenAI enforce it server-side.
            response_format={"type": "json_schema", "json_schema": {"name": schema_name, "schema": schema}},
        )
        return response.choices[0].message.content or ""

    def _tool_step(self, system, history, tools) -> ToolStep:
        kwargs: dict[str, Any] = {}
        if tools:
            kwargs["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
        response = self._client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=[{"role": "system", "content": system}, *history],
            **kwargs,
        )
        message = response.choices[0].message
        calls = []
        for tc in message.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args if isinstance(args, dict) else {}))
        return ToolStep(text=message.content or "", tool_calls=calls, raw_assistant_message=message)

    def _append_tool_results(self, history, step, results) -> None:
        history.append(step.raw_assistant_message.model_dump(exclude_none=True))
        for call, content in results:
            history.append({"role": "tool", "tool_call_id": call.id, "content": content})

    def _user_message(self, content: str) -> Any:
        return {"role": "user", "content": content}
