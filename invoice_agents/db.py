"""SQLite persistence: the mock inventory/vendor master data plus the AP ledger and audit trail.

Run `python -m invoice_agents.db --reset` to (re)create the database with seed data.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import threading
from contextlib import contextmanager, suppress
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
    status TEXT NOT NULL,           -- approved | paid | rejected | pending_review | on_hold | failed
    decision TEXT,
    reasoning TEXT,
    source_file TEXT,
    result_json TEXT,
    llm_calls INTEGER,
    duration_ms INTEGER,
    override_reason TEXT,           -- set when a human reopened a rejected invoice (policy exception)
    processed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- Race-safe duplicate protection: with parallel workers, two copies of one invoice can pass the
-- duplicate *check* at the same moment. The database refuses the second payable record.
CREATE UNIQUE INDEX IF NOT EXISTS one_payable_record_per_invoice
    ON invoices(fingerprint) WHERE status IN ('approved', 'paid', 'pending_review', 'on_hold') AND fingerprint IS NOT NULL;
CREATE INDEX IF NOT EXISTS invoices_by_number ON invoices(invoice_number);
CREATE INDEX IF NOT EXISTS invoices_by_run ON invoices(run_id);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    status TEXT NOT NULL,            -- queued | running | done | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    run_id TEXT,
    invoice_id INTEGER,
    outcome TEXT,
    error TEXT,
    available_at TEXT NOT NULL,
    enqueued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_job_per_file ON jobs(file_hash);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs(status, available_at);
CREATE TABLE IF NOT EXISTS human_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_key TEXT NOT NULL,
    vendor TEXT,
    invoice_number TEXT,
    issue_codes TEXT NOT NULL,       -- JSON list of warning codes the human saw
    decision TEXT NOT NULL,
    note TEXT,
    reviewer TEXT,
    decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS human_decisions_by_vendor ON human_decisions(vendor_key);
-- Every human action on an invoice: approve / reject / hold / reopen. Drives the two-person rule.
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    reviewer TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,           -- approve | reject | hold | reopen
    note TEXT,
    decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS approvals_by_invoice ON approvals(invoice_id);
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
CREATE UNIQUE INDEX IF NOT EXISTS one_payment_per_invoice ON payments(invoice_id);
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


class DuplicateRecord(Exception):
    """A payable record for this invoice already exists (enforced by the database)."""


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
    """SQLite access with one long-lived connection per thread.

    Opening/closing a connection per query is expensive in WAL mode (closing the last connection
    checkpoints the log), and was the single biggest cost at volume. Each worker thread reuses its own
    connection; `connect()` blocks commit as one transaction (nested blocks join the outer one).
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._local = threading.local()
        self._all: list[tuple[threading.Thread, sqlite3.Connection]] = []
        self._all_lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA synchronous = NORMAL")  # standard for WAL: durable across app crashes
            self._local.conn, self._local.depth = conn, 0
            with self._all_lock:
                # Threads come and go (worker pools, a UI thread per page refresh): close connections
                # whose thread has finished so they don't accumulate and pin old WAL snapshots.
                for thread, old in [t for t in self._all if not t[0].is_alive()]:
                    with suppress(sqlite3.ProgrammingError):
                        old.close()
                self._all = [t for t in self._all if t[0].is_alive()]
                self._all.append((threading.current_thread(), conn))
        return conn

    def open_connections(self) -> int:
        with self._all_lock:
            return len(self._all)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        self._local.depth += 1
        try:
            yield conn
            if self._local.depth == 1:
                conn.commit()
        except BaseException:
            if self._local.depth == 1:
                conn.rollback()
            raise
        finally:
            self._local.depth -= 1

    def close(self) -> None:
        with self._all_lock:
            for _, conn in self._all:
                with suppress(sqlite3.ProgrammingError):
                    conn.close()
            self._all.clear()
        self._local = threading.local()

    # ---- setup ---------------------------------------------------------------------------

    def init(self, reset: bool = False) -> None:
        if reset:
            self.close()
            for suffix in ("", "-wal", "-shm"):
                Path(f"{self.path}{suffix}").unlink(missing_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer: needed for parallel workers
            for column in ("llm_calls INTEGER", "duration_ms INTEGER", "override_reason TEXT"):  # upgrade older databases
                with suppress(sqlite3.OperationalError):
                    conn.execute(f"ALTER TABLE invoices ADD COLUMN {column}")
            conn.execute("DROP INDEX IF EXISTS one_payable_record_per_invoice")  # recreated below with current definition
            conn.executescript(SCHEMA)
            conn.executemany("INSERT OR IGNORE INTO inventory VALUES (?, ?, ?, ?, ?)", SEED_INVENTORY)
            conn.executemany(
                "INSERT OR IGNORE INTO vendors (name, aliases, address) VALUES (?, ?, ?)",
                [(n, json.dumps(a), addr) for n, a, addr in SEED_VENDORS],
            )

    def reset_ledger(self) -> None:
        """Clear processed invoices/payments but keep master data."""
        with self.connect() as conn:
            conn.executescript("DELETE FROM payments; DELETE FROM invoices; DELETE FROM audit_log; "
                               "DELETE FROM jobs; DELETE FROM human_decisions; DELETE FROM approvals;")

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
                       fingerprint: str | None, source_file: str | None, result: dict,
                       llm_calls: int | None = None, duration_ms: int | None = None) -> int:
        ts = now_iso()
        try:
            with self.connect() as conn:
                cur = conn.execute(
                    "INSERT INTO invoices (run_id, invoice_number, vendor, total, currency, fingerprint, status, decision, "
                    "reasoning, source_file, result_json, llm_calls, duration_ms, processed_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, invoice.get("invoice_number"), invoice.get("vendor"), invoice.get("total"),
                     invoice.get("currency"), fingerprint, status, decision, reasoning, source_file,
                     json.dumps(result, default=str), llm_calls, duration_ms, ts, ts),
                )
                return int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            if "one_payable_record_per_invoice" in str(exc) or "invoices.fingerprint" in str(exc):
                raise DuplicateRecord(invoice.get("invoice_number")) from exc
            raise

    def invoice_for_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM invoices WHERE run_id = ? ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
        return dict(row) if row else None

    def recent_vendor_invoices(self, vendor: str | None, around: str | None, days: int) -> list[dict[str, Any]]:
        """Payable invoices from the same vendor dated within `days` of `around` (for split-invoice detection)."""
        if not vendor or not around:
            return []
        key = normalize_name(vendor)
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, invoice_number, vendor, total, status, result_json FROM invoices "
                "WHERE status IN ('approved', 'paid', 'pending_review', 'on_hold')")]
        out = []
        for r in rows:
            if normalize_name(r["vendor"]) != key:
                continue
            inv_date = (json.loads(r["result_json"] or "{}").get("invoice") or {}).get("invoice_date")
            if inv_date and abs((datetime.fromisoformat(inv_date) - datetime.fromisoformat(around)).days) <= days:
                out.append({k: r[k] for k in ("id", "invoice_number", "total", "status")} | {"invoice_date": inv_date})
        return out

    # ---- human decision memory ----------------------------------------------------------

    def remember_human_decision(self, *, vendor: str | None, invoice_number: str | None, issue_codes: list[str],
                                decision: str, note: str, reviewer: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO human_decisions (vendor_key, vendor, invoice_number, issue_codes, decision, note, reviewer, decided_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (normalize_name(vendor), vendor, invoice_number, json.dumps(sorted(issue_codes)), decision, note, reviewer, now_iso()),
            )

    def vendor_precedents(self, vendor: str | None, limit: int = 5) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT vendor, invoice_number, issue_codes, decision, note, reviewer, decided_at FROM human_decisions "
                "WHERE vendor_key = ? ORDER BY id DESC LIMIT ?", (normalize_name(vendor), limit)).fetchall()
        return [dict(r) | {"issue_codes": json.loads(r["issue_codes"])} for r in rows]

    def add_approval(self, *, invoice_id: int, reviewer: str, role: str, action: str, note: str = "") -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO approvals (invoice_id, reviewer, role, action, note, decided_at) VALUES (?,?,?,?,?,?)",
                         (invoice_id, reviewer, role, action, note, now_iso()))

    def list_approvals(self, invoice_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM approvals WHERE invoice_id = ? ORDER BY id", (invoice_id,))]

    def all_approvals(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM approvals ORDER BY id DESC")]

    def set_override(self, invoice_id: int, reason: str | None) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE invoices SET override_reason = ?, updated_at = ? WHERE id = ?", (reason, now_iso(), invoice_id))

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
            return conn.execute("SELECT 1 FROM payments WHERE invoice_id = ? AND status = 'success'", (invoice_id,)).fetchone() is not None

    def claim_payment(self, *, invoice_id: int, invoice_number: str | None, vendor: str, amount: float,
                      currency: str, reference: str) -> bool:
        """Reserve the payment slot for an invoice *before* calling the bank. False if already claimed."""
        try:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO payments (invoice_id, invoice_number, vendor, amount, currency, reference, status, paid_at) "
                    "VALUES (?,?,?,?,?,?,'processing',?)",
                    (invoice_id, invoice_number, vendor, amount, currency, reference, now_iso()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def finish_payment(self, reference: str, status: str) -> None:
        with self.connect() as conn:
            if status == "success":
                conn.execute("UPDATE payments SET status = ?, paid_at = ? WHERE reference = ?", (status, now_iso(), reference))
            else:  # free the slot so a later retry can pay
                conn.execute("DELETE FROM payments WHERE reference = ?", (reference,))

    def payment_for_invoice(self, invoice_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM payments WHERE invoice_id = ?", (invoice_id,)).fetchone()
        return dict(row) if row else None

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

    def audit_many(self, events: list[dict[str, Any]]) -> None:
        """One transaction per invoice instead of one per event - matters at volume."""
        if not events:
            return
        ts = now_iso()
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO audit_log (ts, run_id, invoice_number, stage, event, detail) VALUES (?,?,?,?,?,?)",
                [(ts, e["run_id"], e["invoice_number"], e["stage"], e["event"], json.dumps(e["detail"], default=str)) for e in events],
            )

    def set_run_metrics(self, invoice_id: int, *, llm_calls: int, duration_ms: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE invoices SET llm_calls = ?, duration_ms = ? WHERE id = ?", (llm_calls, duration_ms, invoice_id))

    def audit_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE run_id = ? ORDER BY id", (run_id,))]

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
