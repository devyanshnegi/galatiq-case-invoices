"""Where the agents' tools live: in-process (`local`) or behind MCP servers (`mcp`).

Agents only see `Backend.reviewer_tools()` and `Backend.pay()`. With TOOLS_BACKEND=mcp the ERP lookups
and the bank are separate MCP servers, so swapping the SQLite mock for a real ERP or bank means
replacing a server - the same way the LLM is swapped by config.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
from typing import Any, Protocol

from .bank import pay as bank_pay
from .config import Settings
from .db import Store
from .erp import ERP_TOOLS
from .llm import Tool

log = logging.getLogger(__name__)


class Backend(Protocol):
    name: str

    def reviewer_tools(self) -> list[Tool]: ...

    def pay(self, *, invoice_id: int, invoice_number: str | None, vendor: str, amount: float, currency: str) -> dict: ...

    def close(self) -> None: ...


class LocalBackend:
    name = "local"

    def __init__(self, settings: Settings, store: Store, quiet: bool = False):
        self.settings, self.store, self.quiet = settings, store, quiet

    def reviewer_tools(self) -> list[Tool]:
        return [Tool(name, desc, params, lambda _fn=fn, **args: _fn(self.store, self.settings, **args))
                for name, desc, params, fn in ERP_TOOLS]

    def pay(self, **kwargs) -> dict:
        return bank_pay(self.store, single_payment_limit=self.settings.bank_single_payment_limit, quiet=self.quiet, **kwargs)

    def close(self) -> None:
        pass


class MCPBackend:
    """Talks to the acme-erp and acme-bank MCP servers.

    The MCP client is async; the agents are sync and may run on many worker threads. One background
    event loop owns both sessions for their whole life, and worker threads submit calls into it.
    """

    name = "mcp"

    def __init__(self, settings: Settings, erp_target: Any = None, bank_target: Any = None, timeout_s: float = 60):
        from mcp import Client, StdioServerParameters

        def stdio(module: str):
            env = {"INVENTORY_DB": str(settings.db_path), "BANK_SINGLE_PAYMENT_LIMIT": str(settings.bank_single_payment_limit)}
            return StdioServerParameters(command=sys.executable, args=["-m", module], env=env)

        self._targets = (erp_target or stdio("invoice_agents.mcp_servers.erp"), bank_target or stdio("invoice_agents.mcp_servers.bank"))
        self._Client = Client
        self._timeout = timeout_s
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="mcp-client", daemon=True)
        self._thread.start()
        self._ready = threading.Event()
        self._closing: asyncio.Event | None = None
        self._error: BaseException | None = None
        self._holder = asyncio.run_coroutine_threadsafe(self._hold(), self._loop)
        if not self._ready.wait(timeout_s):
            raise RuntimeError("MCP servers did not start in time")
        if self._error:
            raise RuntimeError(f"could not connect to MCP servers: {self._error}") from self._error

    async def _hold(self) -> None:
        # Sessions are entered and exited inside this one task, as anyio requires.
        self._closing = asyncio.Event()
        try:
            async with self._Client(self._targets[0]) as erp, self._Client(self._targets[1]) as bank:
                self.erp, self.bank = erp, bank
                self._erp_tools = (await erp.list_tools()).tools
                self._ready.set()
                await self._closing.wait()
        except BaseException as exc:  # surface startup failures to the constructor
            self._error = exc
            self._ready.set()
            raise

    def _call(self, client: Any, name: str, arguments: dict) -> Any:
        future = asyncio.run_coroutine_threadsafe(client.call_tool(name, arguments), self._loop)
        result = future.result(self._timeout)
        text = "".join(getattr(c, "text", "") for c in result.content)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = text
        return {"error": data} if result.is_error else data

    def reviewer_tools(self) -> list[Tool]:
        return [Tool(t.name, t.description or t.name, t.input_schema, lambda _n=t.name, **args: self._call(self.erp, _n, args))
                for t in self._erp_tools]

    def pay(self, **kwargs) -> dict:
        result = self._call(self.bank, "pay_invoice", kwargs)
        return result if isinstance(result, dict) and "status" in result else {"status": "error", "reason": str(result)}

    def close(self) -> None:
        if self._closing is not None:
            self._loop.call_soon_threadsafe(self._closing.set)
            try:
                self._holder.result(10)
            except Exception as exc:  # shutting down anyway
                log.debug("MCP shutdown: %s", exc)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)


def create_backend(settings: Settings, store: Store, quiet: bool = False) -> Backend:
    if settings.tools_backend == "mcp":
        return MCPBackend(settings)
    if settings.tools_backend != "local":
        raise ValueError(f"Unknown TOOLS_BACKEND '{settings.tools_backend}' (use local or mcp)")
    return LocalBackend(settings, store, quiet=quiet)
