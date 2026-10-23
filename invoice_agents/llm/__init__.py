"""LLM provider factory. Swap models by changing LLM_PROVIDER / LLM_MODEL - no code changes."""

from __future__ import annotations

import logging
import urllib.request

from ..config import LLMSettings
from .base import LLMClient, LLMError, Tool, ToolLoopResult

log = logging.getLogger(__name__)

DEFAULT_MODELS = {
    "ollama": "qwen2.5:7b",
    "xai": "grok-3",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-5-5",
    "offline": "rules",
}

XAI_BASE_URL = "https://api.x.ai/v1"


class OfflineClient(LLMClient):
    """No model at all. Agents detect `is_offline` and use their deterministic fallbacks.

    Lets the whole pipeline (and the test-suite) run with zero setup, and is what we degrade
    to if the configured model is unreachable.
    """

    provider = "offline"
    is_offline = True

    def _complete_json(self, *args, **kwargs):
        raise LLMError("offline mode: no model configured")

    def _tool_step(self, *args, **kwargs):
        raise LLMError("offline mode: no model configured")

    def _append_tool_results(self, *args, **kwargs):
        raise LLMError("offline mode: no model configured")

    def _user_message(self, content):
        return {"role": "user", "content": content}


def _ollama_is_up(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/models", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def create_llm(settings: LLMSettings) -> LLMClient:
    provider = settings.provider
    if provider not in DEFAULT_MODELS:
        raise ValueError(f"Unknown LLM_PROVIDER '{provider}'. Choose one of: {', '.join(DEFAULT_MODELS)}")
    model = settings.model or DEFAULT_MODELS[provider]
    common = {"temperature": settings.temperature, "timeout_s": settings.timeout_s}

    problem: str | None = None
    client: LLMClient | None = None
    if provider == "ollama":
        if _ollama_is_up(settings.ollama_base_url):
            from .openai_compat import OpenAICompatibleClient

            client = OpenAICompatibleClient(provider="ollama", model=model, base_url=settings.ollama_base_url, api_key="ollama", **common)
        else:
            problem = f"Ollama is not reachable at {settings.ollama_base_url} (is `ollama serve` running?)"
    elif provider == "xai":
        if settings.xai_api_key:
            from .openai_compat import OpenAICompatibleClient

            client = OpenAICompatibleClient(provider="xai", model=model, base_url=XAI_BASE_URL, api_key=settings.xai_api_key, **common)
        else:
            problem = "XAI_API_KEY is not set"
    elif provider == "openai":
        if settings.openai_api_key:
            from .openai_compat import OpenAICompatibleClient

            client = OpenAICompatibleClient(provider="openai", model=model, base_url=None, api_key=settings.openai_api_key, **common)
        else:
            problem = "OPENAI_API_KEY is not set"
    elif provider == "anthropic":
        if settings.anthropic_api_key:
            from .anthropic_client import AnthropicClient

            client = AnthropicClient(model=model, api_key=settings.anthropic_api_key, **common)
        else:
            problem = "ANTHROPIC_API_KEY is not set"
    else:
        client = OfflineClient(model, **common)

    if client is not None:
        return client
    if not settings.fallback_to_offline:
        raise LLMError(problem)
    log.warning("%s - falling back to offline (rule-based) mode.", problem)
    return OfflineClient(DEFAULT_MODELS["offline"], **common)


__all__ = ["LLMClient", "LLMError", "Tool", "ToolLoopResult", "OfflineClient", "create_llm", "DEFAULT_MODELS"]
