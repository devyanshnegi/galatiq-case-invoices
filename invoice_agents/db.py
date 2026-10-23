"""SQLite persistence: the mock inventory/vendor master data plus the AP ledger and audit trail.

Run `python -m invoice_agents.db --reset` to (re)create the database with seed data.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterator

# The case's required minimum (item, stock), extended with catalog price and category so we can
# also catch price gouging.
SEED_INVENTORY = [
    ("WidgetA", 15, 250.00, "Widgets", "Standard widget, type A"),
    ("WidgetB", 10, 500.00, "Widgets", "Standard widget, type B"),
    ("GadgetX", 5, 750.00, "Gadgets", "Precision gadget, model X"),
    ("FakeItem", 0, 0.00, "Discontinued", "Not a real SKU - zero stock, never orderable"),
]

# Approved vendor master. Vendors not on this list are not blocked, but are flagged for review.
SEED_VENDORS = [
    ("Widgets Inc.", [], "100 Main St, Chicago, IL 60601"),
    ("Gadgets Co.", [], None),
    ("Precision Parts Ltd.", [], "742 Evergreen Terrace, Springfield, IL 62704"),
    ("Acme Industrial Supplies", [], None),
    ("MegaWidgets Corp", [], None),
    ("Consolidated Materials Group", [], None),
    ("Summit Manufacturing Co.", [], None),
    ("QuickShip Distributors", ["FastShip Ltd."], None),
    ("Atlas Industrial Supply", [], "500 Commerce Blvd, Detroit, MI 48201"),
    ("TechParts International", [], None),
    ("Reliable Components Inc.", [], None),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (
    item TEXT PRIMARY KEY,
    stock INTEGER NOT NULL,
    unit_price REAL,
    category TEXT,
    description TEXT
);
CREATE TABLE IF NOT EXISTS vendors (
    name TEXT PRIMARY KEY,
    aliases TEXT NOT NULL DEFAULT '[]',
    address TEXT,
    status TEXT NOT NULL DEFAULT 'approved'
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    invoice_number TEXT,
    vendor TEXT,
    total REAL,
    currency TEXT,
    fingerprint TEXT,
    status TEXT NOT NULL,           -- paid | rejected | pending_review | failed
    decision TEXT,
    reasoning TEXT,
    source_file TEXT,
    result_json TEXT,
    processed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER REFERENCES invoices(id),
    invoice_number TEXT,
    vendor TEXT,
    amount REAL,
    currency TEXT,
    reference TEXT,
    status TEXT,
    paid_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    run_id TEXT,
    invoice_number TEXT,
    stage TEXT,
    event TEXT,
    detail TEXT
);
"""

_SUFFIXES = re.compile(r"\b(inc|incorporated|co|corp|corporation|company|ltd|limited|llc|plc|group)\b\.?")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_name(name: str | None) -> str:
    """'Widgets Inc.' and 'WIDGETS, INC' compare equal."""
    if not name:
        return ""
    name = _SUFFIXES.sub("", name.lower())
    return re.sub(r"[^a-z0-9]", "", name)


def normalize_sku(name: str | None) -> str:
    """'Widget A', 'widget-a', 'WidgetA (rush order)' -> 'widgeta'."""
    if not name:
        return ""
    name = re.sub(r"\(.*?\)", "", name)
    return re.sub(r"[^a-z0-9]", "", name.lower())


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- setup ---------------------------------------------------------------------------

    def init(self, reset: bool = False) -> None:
        if reset and self.path.exists():
            self.path.unlink()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.executemany("INSERT OR IGNORE INTO inventory VALUES (?, ?, ?, ?, ?)", SEED_INVENTORY)
            conn.executemany(
                "INSERT OR IGNORE INTO vendors (name, aliases, address) VALUES (?, ?, ?)",
                [(n, json.dumps(a), addr) for n, a, addr in SEED_VENDORS],
            )

    def reset_ledger(self) -> None:
        """Clear processed invoices/payments but keep master data."""
        with self.connect() as conn:
            conn.executescript("DELETE FROM payments; DELETE FROM invoices; DELETE FROM audit_log;")

    # ---- master data ---------------------------------------------------------------------

    def list_inventory(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM inventory ORDER BY item")]

    def match_item(self, name: str | None) -> dict[str, Any] | None:
        """Find the catalog SKU for a free-text item name (tolerates spacing, case, annotations).

        Deliberately no fuzzy matching: SKUs like WidgetA/WidgetB/WidgetC differ by one
        character, so a 'close enough' match would silently pay for the wrong product.
        """
        key = normalize_sku(name)
        if not key:
            return None
        for row in self.list_inventory():
            if normalize_sku(row["item"]) == key:
                return row
        return None

    def find_vendor(self, name: str | None) -> tuple[dict[str, Any] | None, str]:
        """Return (vendor_row, match_type) where match_type is exact | alias | fuzzy | none."""
        key = normalize_name(name)
        if not key:
            return None, "none"
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM vendors")]
        for row in rows:
            row["aliases"] = json.loads(row["aliases"])
            if normalize_name(row["name"]) == key:
                return row, "exact"
        for row in rows:
            if any(normalize_name(a) == key for a in row["aliases"]):
                return row, "alias"
        scored = [(SequenceMatcher(None, normalize_name(r["name"]), key).ratio(), r) for r in rows]
        score, best = max(scored, key=lambda s: s[0], default=(0, None))
        if best and score >= 0.85:
            return best, "fuzzy"
        return None, "none"

    # ---- ledger --------------------------------------------------------------------------

    def find_prior_invoices(self, invoice_number: str | None, vendor: str | None) -> list[dict[str, Any]]:
        """Previously processed invoices with the same number (any vendor) or same vendor+number."""
        if not invoice_number:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, invoice_number, vendor, total, fingerprint, status, source_file, processed_at "
                "FROM invoices WHERE invoice_number = ? ORDER BY id",
                (invoice_number,),
            ).fetchall()
        return [dict(r) for r in rows]

    def vendor_history(self, vendor: str | None) -> dict[str, Any]:
        key = normalize_name(vendor)
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT vendor, status, total FROM invoices")]
        mine = [r for r in rows if normalize_name(r["vendor"]) == key]
        return {
            "invoices_processed": len(mine),
            "paid": sum(1 for r in mine if r["status"] == "paid"),
            "rejected": sum(1 for r in mine if r["status"] == "rejected"),
            "total_paid": round(sum(r["total"] or 0 for r in mine if r["status"] == "paid"), 2),
        }

    def record_invoice(self, *, run_id: str, invoice: dict, status: str, decision: str | None, reasoning: str | None,
                       fingerprint: str | None, source_file: str | None, result: dict) -> int:
        ts = now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO invoices (run_id, invoice_number, vendor, total, currency, fingerprint, status, decision, "
                "reasoning, source_file, result_json, processed_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, invoice.get("invoice_number"), invoice.get("vendor"), invoice.get("total"),
                 invoice.get("currency"), fingerprint, status, decision, reasoning, source_file,
                 json.dumps(result, default=str), ts, ts),
            )
            return int(cur.lastrowid)

    def update_invoice_status(self, invoice_id: int, status: str, decision: str, reasoning: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE invoices SET status = ?, decision = ?, reasoning = ?, updated_at = ? WHERE id = ?",
                (status, decision, reasoning, now_iso(), invoice_id),
            )

    def get_invoice(self, invoice_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        return dict(row) if row else None

    def list_invoices(self, status: str | None = None) -> list[dict[str, Any]]:
        query, args = "SELECT * FROM invoices", ()
        if status:
            query, args = query + " WHERE status = ?", (status,)
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(query + " ORDER BY id DESC", args)]

    def is_paid(self, invoice_id: int) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM payments WHERE invoice_id = ?", (invoice_id,)).fetchone() is not None

    def record_payment(self, *, invoice_id: int, invoice_number: str | None, vendor: str, amount: float,
                       currency: str, reference: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO payments (invoice_id, invoice_number, vendor, amount, currency, reference, status, paid_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (invoice_id, invoice_number, vendor, amount, currency, reference, status, now_iso()),
            )

    def list_payments(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM payments ORDER BY id DESC")]

    # ---- audit ---------------------------------------------------------------------------

    def audit(self, *, run_id: str, invoice_number: str | None, stage: str, event: str, detail: Any = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, run_id, invoice_number, stage, event, detail) VALUES (?,?,?,?,?,?)",
                (now_iso(), run_id, invoice_number, stage, event, json.dumps(detail, default=str)),
            )

    def list_audit(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]


def main() -> None:
    from .config import load_settings

    parser = argparse.ArgumentParser(description="Create / reset the mock inventory database.")
    parser.add_argument("--reset", action="store_true", help="delete and recreate the database")
    parser.add_argument("--db", default=None, help="database path (default: inventory.db)")
    args = parser.parse_args()
    path = args.db or load_settings().db_path
    store = Store(path)
    store.init(reset=args.reset)
    print(f"Inventory DB ready at {path}")
    for row in store.list_inventory():
        print(f"  {row['item']:<10} stock={row['stock']:<4} price=${row['unit_price']:,.2f}")


if __name__ == "__main__":
    main()
