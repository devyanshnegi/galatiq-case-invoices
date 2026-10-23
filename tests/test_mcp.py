"""MCP integration: the agents' ERP/bank tools served over MCP, and the AP system exposed as an MCP server."""

import asyncio
import contextlib
import io
import json
from collections import Counter
from dataclasses import replace

import pytest

from invoice_agents.backends import MCPBackend
from invoice_agents.graph import InvoicePipeline
from invoice_agents.llm.base import ToolCall, ToolStep
from invoice_agents.mcp_servers import ap, bank, erp

from .conftest import DATA, FakeLLM
from .test_pipeline import EXPECTED


@pytest.fixture
def mcp_backend(settings, store):
    backend = MCPBackend(settings, erp_target=erp.build_server(settings, store), bank_target=bank.build_server(settings, store))
    yield backend
    backend.close()


def test_erp_tools_are_discovered_over_mcp(mcp_backend):
    tools = {t.name: t for t in mcp_backend.reviewer_tools()}
    assert set(tools) == {"get_approval_policy", "lookup_inventory", "get_vendor_profile", "get_vendor_history",
                          "find_prior_invoices", "get_vendor_precedents"}
    assert tools["lookup_inventory"].invoke({"item": "Gadget X"})["stock"] == 5
    assert "APPROVAL POLICY" in tools["get_approval_policy"].invoke({})


def test_bank_over_mcp_is_idempotent_and_enforces_limit(mcp_backend):
    with contextlib.redirect_stdout(io.StringIO()):
        first = mcp_backend.pay(invoice_id=1, invoice_number="INV-1", vendor="V", amount=10.0, currency="USD")
        again = mcp_backend.pay(invoice_id=1, invoice_number="INV-1", vendor="V", amount=10.0, currency="USD")
        big = mcp_backend.pay(invoice_id=2, invoice_number="INV-2", vendor="V", amount=1e9, currency="USD")
    assert (first["status"], again["status"], big["status"]) == ("success", "skipped", "declined")


def test_full_pipeline_over_mcp_matches_local(settings, mcp_backend):
    with contextlib.redirect_stdout(io.StringIO()):
        pipeline = InvoicePipeline(settings, backend=mcp_backend)
        statuses = {n: pipeline.process(DATA / n)["status"] for n in EXPECTED}
    assert statuses == EXPECTED
    assert all(e["detail"].get("backend") == "mcp" for s in [pipeline.process(DATA / "invoice_1015.csv")]
               for e in s["trace"] if e["event"] == "payment_executed")


def test_reviewer_tool_calls_go_through_mcp(settings, mcp_backend):
    settings = replace(settings, fast_path=False)
    steps = [ToolStep(tool_calls=[ToolCall("1", "get_vendor_profile", {"vendor": "Widgets Inc."})]), ToolStep(text="Vendor approved.")]
    good = {"invoice_number": "INV-1001", "vendor": "Widgets Inc.", "invoice_date": "2026-01-15", "due_date": "2026-02-01",
            "line_items": [{"item": "WidgetA", "quantity": 10, "unit_price": 250}, {"item": "WidgetB", "quantity": 5, "unit_price": 500}],
            "subtotal": 5000, "tax": 0, "total": 5000, "payment_terms": "Net 15"}
    llm = FakeLLM({"Invoice": [good], "RiskAssessment": [{"suspicious": False, "signals": []}],
                   "ApprovalDecision": [{"decision": "approve", "reasoning": "Clean.", "key_factors": [], "conditions": []}],
                   "Critique": [{"agrees": True, "problems": [], "recommended_decision": "approve"}]}, tool_steps=steps)
    with contextlib.redirect_stdout(io.StringIO()):
        state = InvoicePipeline(settings, llm=llm, backend=mcp_backend).process(DATA / "invoice_1001.txt")
    assert '"match": "exact"' in llm.prompts["ApprovalDecision"][0]  # the MCP server's answer reached the reviewer
    assert state["status"] == "paid"


def test_real_stdio_servers_end_to_end(settings, store):
    """Spawns the actual acme-erp and acme-bank server processes."""
    with contextlib.redirect_stdout(io.StringIO()), InvoicePipeline(replace(settings, tools_backend="mcp"), store=store) as pipeline:
        assert pipeline.backend.name == "mcp"
        state = pipeline.process(DATA / "invoice_1001.txt")
    assert state["status"] == "paid" and len(store.list_payments()) == 1


def test_ap_server_runs_the_whole_workflow(settings):
    async def scenario(server):
        from mcp import Client

        async with Client(server) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert {"process_invoice", "process_folder", "list_pending", "resolve_invoice", "ledger_summary"} <= names

            async def call(name, **args):
                result = await client.call_tool(name, args)
                return json.loads("".join(c.text for c in result.content))

            folder = await call("process_folder", folder=str(DATA), workers=4)
            pending = await call("list_pending")
            fx = next(p for p in pending if p["currency"] == "EUR")
            resolved = await call("resolve_invoice", invoice_id=fx["invoice_id"], approve=True, reviewer="VP", note="FX ok")
            again = await call("resolve_invoice", invoice_id=fx["invoice_id"], approve=True, reviewer="VP")
            summary = await call("ledger_summary")
            return folder, pending, resolved, again, summary

    with contextlib.redirect_stdout(io.StringIO()), InvoicePipeline(settings, quiet_payments=True) as pipeline:
        folder, pending, resolved, again, summary = asyncio.run(scenario(ap.build_server(pipeline, allowed_root=DATA.parent.parent)))
    assert folder["queued"] == 20 and Counter(folder["outcomes"]) == Counter(EXPECTED.values())
    assert len(pending) == 3 and resolved["payment"]["status"] == "success"
    assert "error" in again  # can't approve twice
    assert summary["by_status"]["paid"] == 7


def test_ap_server_refuses_paths_outside_project(settings, tmp_path):
    async def scenario(server):
        from mcp import Client

        async with Client(server) as client:
            return await client.call_tool("process_invoice", {"path": "../../Windows/win.ini"})

    with InvoicePipeline(settings, quiet_payments=True) as pipeline:
        result = asyncio.run(scenario(ap.build_server(pipeline, allowed_root=tmp_path)))
    assert result.is_error
