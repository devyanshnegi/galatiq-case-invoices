"""ERP lookups the reviewer agent can call. Served in-process or by the `acme-erp` MCP server.

Each spec is (name, description, JSON-schema parameters, function(store, settings, **args)).
"""

from __future__ import annotations

from typing import Any, Callable

from .config import Settings
from .db import Store
from .policy import policy_text

_NO_ARGS = {"type": "object", "properties": {}, "required": []}


def _one(name: str) -> dict:
    return {"type": "object", "properties": {name: {"type": "string"}}, "required": [name]}


def get_approval_policy(store: Store, settings: Settings) -> str:
    return policy_text(settings.policy)


def lookup_inventory(store: Store, settings: Settings, item: str) -> Any:
    return store.match_item(item) or {"found": False, "item": item, "note": "not in inventory catalog"}


def get_vendor_profile(store: Store, settings: Settings, vendor: str) -> Any:
    row, match = store.find_vendor(vendor)
    return {"match": match, "vendor": row} if row else {"match": "none", "note": f"'{vendor}' is not in the approved vendor master"}


def get_vendor_history(store: Store, settings: Settings, vendor: str) -> Any:
    return store.vendor_history(vendor)


def find_prior_invoices(store: Store, settings: Settings, invoice_number: str) -> Any:
    return store.find_prior_invoices(invoice_number, None) or {"note": "no prior invoices with this number"}


def get_vendor_precedents(store: Store, settings: Settings, vendor: str) -> Any:
    return store.vendor_precedents(vendor) or {"note": "no previous human decisions for this vendor"}


ERP_TOOLS: list[tuple[str, str, dict, Callable[..., Any]]] = [
    ("get_approval_policy", "Get Acme Corp's binding invoice-approval policy.", _NO_ARGS, get_approval_policy),
    ("lookup_inventory", "Look up an item's stock level and catalog price.", _one("item"), lookup_inventory),
    ("get_vendor_profile", "Check whether a vendor is in the approved vendor master, with address on file.", _one("vendor"), get_vendor_profile),
    ("get_vendor_history", "Count of past invoices paid/rejected for a vendor and total paid.", _one("vendor"), get_vendor_history),
    ("find_prior_invoices", "Find previously processed invoices with the same invoice number.", _one("invoice_number"), find_prior_invoices),
    ("get_vendor_precedents", "How humans decided this vendor's previous escalated invoices, with their notes.", _one("vendor"), get_vendor_precedents),
]
