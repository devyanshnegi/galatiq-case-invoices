"""acme-erp MCP server: inventory, vendor master, invoice history and human-decision memory.

    python -m invoice_agents.mcp_servers.erp        (stdio)
"""

from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer

from .. import erp
from ..config import Settings, load_settings
from ..db import Store


def build_server(settings: Settings, store: Store) -> MCPServer:
    server = MCPServer("acme-erp", instructions="Read-only lookups against Acme Corp's ERP: inventory, vendors, invoice history.")

    def out(value) -> str:
        return value if isinstance(value, str) else json.dumps(value, default=str)

    @server.tool(description="Get Acme Corp's binding invoice-approval policy.")
    def get_approval_policy() -> str:
        return out(erp.get_approval_policy(store, settings))

    @server.tool(description="Look up an item's stock level and catalog price.")
    def lookup_inventory(item: str) -> str:
        return out(erp.lookup_inventory(store, settings, item))

    @server.tool(description="Check whether a vendor is in the approved vendor master, with address on file.")
    def get_vendor_profile(vendor: str) -> str:
        return out(erp.get_vendor_profile(store, settings, vendor))

    @server.tool(description="Count of past invoices paid/rejected for a vendor and total paid.")
    def get_vendor_history(vendor: str) -> str:
        return out(erp.get_vendor_history(store, settings, vendor))

    @server.tool(description="Find previously processed invoices with the same invoice number.")
    def find_prior_invoices(invoice_number: str) -> str:
        return out(erp.find_prior_invoices(store, settings, invoice_number))

    @server.tool(description="How humans decided this vendor's previous escalated invoices, with their notes.")
    def get_vendor_precedents(vendor: str) -> str:
        return out(erp.get_vendor_precedents(store, settings, vendor))

    return server


def main() -> None:
    settings = load_settings()
    store = Store(settings.db_path)
    store.init()
    build_server(settings, store).run()


if __name__ == "__main__":
    main()
