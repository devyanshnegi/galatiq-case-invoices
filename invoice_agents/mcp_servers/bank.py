"""acme-bank MCP server: the only way money moves.

Idempotency (one payment per invoice) and the single-payment limit are enforced here, at the bank,
so no agent - however it's prompted - can bypass them.

    python -m invoice_agents.mcp_servers.bank       (stdio)
"""

from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer

from .. import bank
from ..config import Settings, load_settings
from ..db import Store


def build_server(settings: Settings, store: Store) -> MCPServer:
    server = MCPServer("acme-bank", instructions="Mock banking API. Pays approved invoices exactly once, within limits.")

    @server.tool(description="Pay an approved invoice. Idempotent per invoice_id; declines amounts over the single-payment limit.")
    def pay_invoice(invoice_id: int, vendor: str, amount: float, currency: str = "USD", invoice_number: str | None = None) -> str:
        result = bank.pay(store, invoice_id=invoice_id, invoice_number=invoice_number, vendor=vendor, amount=amount,
                          currency=currency, single_payment_limit=settings.bank_single_payment_limit, quiet=True)
        return json.dumps(result)

    @server.tool(description="Look up the payment made for an invoice, if any.")
    def get_payment(invoice_id: int) -> str:
        return json.dumps(store.payment_for_invoice(invoice_id) or {"note": "no payment for this invoice"})

    return server


def main() -> None:
    settings = load_settings()
    store = Store(settings.db_path)
    store.init()
    build_server(settings, store).run()


if __name__ == "__main__":
    main()
