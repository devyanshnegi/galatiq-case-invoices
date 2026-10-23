"""Synthetic invoice generator with ground truth, for load testing.

Each invoice is built from a scenario (clean, over-stock, bad maths, look-alike vendor, ...) so we
know the correct outcome in advance. Formats are mixed (TXT / JSON / CSV) like a real inbox.

    python -m invoice_agents.synthetic --n 1000 --out data/synthetic
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .db import SEED_INVENTORY, SEED_VENDORS

CATALOG = {item: (stock, price) for item, stock, price, *_ in SEED_INVENTORY if stock > 0}
VENDORS = [name for name, _, _ in SEED_VENDORS]
ADDRESSES = {name: addr for name, _, addr in SEED_VENDORS if addr}

# (scenario, weight, acceptable outcomes). "dup" scenarios emit two files; exactly one may be paid.
SCENARIOS = [
    ("clean", 46, {"paid"}),
    ("clean_large", 6, {"paid", "pending_review"}),     # VP tier: approve or VP asks for a human look
    ("documented_premium", 4, {"paid", "pending_review"}),  # rush premium: reviewer's judgement
    ("over_stock", 7, {"rejected"}),
    ("split_stock", 3, {"rejected"}),                   # each line fine, total over stock
    ("unknown_item", 5, {"rejected"}),
    ("bad_total", 5, {"rejected"}),
    ("negative_qty", 2, {"rejected"}),
    ("missing_due", 2, {"rejected"}),
    ("unknown_vendor_pressure", 4, {"pending_review", "rejected"}),
    ("lookalike_vendor", 3, {"pending_review", "rejected"}),
    ("address_change", 3, {"pending_review", "rejected"}),
    ("foreign_currency", 3, {"pending_review"}),
    ("duplicate_pair", 7, None),                        # group rule: exactly one paid, the other rejected
    ("split_pair", 3, None),                            # group rule: exactly one paid, the other held for review
]


@dataclass
class Spec:
    number: str
    vendor: str
    issued: date
    due: date | None
    lines: list[tuple[str, int, float, str | None]]
    tax_rate: float = 0.0
    currency: str = "USD"
    address: str | None = None
    notes: str | None = None
    total_override: float | None = None

    @property
    def subtotal(self) -> float:
        return round(sum(q * p for _, q, p, _ in self.lines), 2)

    @property
    def tax(self) -> float:
        return round(self.subtotal * self.tax_rate, 2)

    @property
    def total(self) -> float:
        return self.total_override if self.total_override is not None else round(self.subtotal + self.tax, 2)


SMALL_INVOICE_CAP = 4_500.0  # typical invoices stay well under the near-threshold band used by split detection


def _clean_lines(rng: random.Random) -> list[tuple[str, int, float, str | None]]:
    items = rng.sample(list(CATALOG), k=rng.randint(1, len(CATALOG)))
    lines = [[item, rng.randint(1, max(1, CATALOG[item][0] // 2)), CATALOG[item][1], None] for item in items]
    while sum(q * p for _, q, p, _ in lines) > SMALL_INVOICE_CAP:
        biggest = max((ln for ln in lines if ln[1] > 1), key=lambda ln: ln[1] * ln[2], default=None)
        if biggest is None:
            break
        biggest[1] -= 1
    return [tuple(ln) for ln in lines]


def _spec(rng: random.Random, n: int, scenario: str) -> Spec:
    issued = date(2026, 9, 1) + timedelta(days=rng.randint(0, 40))
    vendor = rng.choice(VENDORS)
    spec = Spec(number=f"INV-{50000 + n}", vendor=vendor, issued=issued, due=issued + timedelta(days=30),
                lines=_clean_lines(rng), tax_rate=rng.choice([0.0, 0.05, 0.06, 0.08]), address=ADDRESSES.get(vendor))
    if scenario == "clean_large":
        spec.lines = [(item, stock - rng.randint(0, 1), price, None) for item, (stock, price) in CATALOG.items()]
    elif scenario == "documented_premium":
        item = rng.choice(list(CATALOG))
        spec.lines = [ln for ln in spec.lines if ln[0] != item][:1]
        spec.lines.append((item, 1, round(CATALOG[item][1] * 1.2, 2), "rush order"))
        spec.lines = _fit_stock(spec.lines)
    elif scenario == "over_stock":
        item = rng.choice(list(CATALOG))
        spec.lines = [(item, CATALOG[item][0] + rng.randint(1, 20), CATALOG[item][1], None)]
    elif scenario == "split_stock":
        item = rng.choice(list(CATALOG))
        stock, price = CATALOG[item]
        spec.lines = [(item, stock, price, None), (item, rng.randint(1, 3), price, "additional")]
    elif scenario == "unknown_item":
        spec.lines.append((rng.choice(["WidgetC", "GizmoPro", "SprocketMax", "WidgetZ"]), rng.randint(1, 5), 300.0, None))
    elif scenario == "bad_total":
        spec.total_override = round(spec.subtotal + spec.tax + rng.choice([50, 110, -75, 999]), 2)
    elif scenario == "negative_qty":
        item, _, price, _ = spec.lines[0]
        spec.lines[0] = (item, -rng.randint(1, 5), price, None)
    elif scenario == "missing_due":
        spec.due = None
    elif scenario == "unknown_vendor_pressure":
        spec.vendor = rng.choice(["Rapid Settlement Partners", "Global Invoice Clearing LLC", "Prime Vendor Services"])
        spec.address = None
        spec.notes = "URGENT - pay immediately by wire transfer to avoid penalties."
    elif scenario == "lookalike_vendor":
        spec.vendor = {"Widgets Inc.": "Widgetts Inc.", "Atlas Industrial Supply": "Atlas Industrial Suply"}.get(
            vendor, vendor.replace("o", "0", 1) if "o" in vendor else vendor + "s")
        spec.address = None
    elif scenario == "address_change":
        spec.vendor = rng.choice(list(ADDRESSES))
        spec.address = "PO Box 4471, Road Town, Tortola, BVI"
    elif scenario == "foreign_currency":
        spec.currency = "EUR"
    return spec


def _fit_stock(lines):
    used: dict[str, int] = {}
    out = []
    for item, qty, price, note in lines:
        allowed = CATALOG[item][0] - used.get(item, 0)
        if allowed <= 0:
            continue
        qty = min(qty, allowed)
        used[item] = used.get(item, 0) + qty
        out.append((item, qty, price, note))
    return out


# ---- renderers ----------------------------------------------------------------------------------


def render_txt(s: Spec) -> str:
    cur = "$" if s.currency == "USD" else ""
    suffix = "" if s.currency == "USD" else f" {s.currency}"
    out = ["INVOICE", "", f"Vendor: {s.vendor}", f"Invoice Number: {s.number}", f"Date: {s.issued.isoformat()}",
           f"Due Date: {s.due.isoformat() if s.due else ''}", "", "Items:"]
    for item, qty, price, note in s.lines:
        name = f"{item} ({note})" if note else item
        out.append(f"  {name:<22} qty: {qty:<4} unit price: {cur}{price:,.2f}{suffix}")
    out += ["", f"Subtotal: {cur}{s.subtotal:,.2f}{suffix}", f"Tax ({s.tax_rate:.0%}): {cur}{s.tax:,.2f}{suffix}",
            f"Total Amount: {cur}{s.total:,.2f}{suffix}", "", "Payment Terms: Net 30"]
    if s.notes:
        out.append(f"Notes: {s.notes}")
    return "\n".join(out) + "\n"


def render_json(s: Spec) -> str:
    return json.dumps({
        "invoice_number": s.number,
        "vendor": {"name": s.vendor, "address": s.address},
        "date": s.issued.isoformat(),
        "due_date": s.due.isoformat() if s.due else None,
        "line_items": [{"item": i, "quantity": q, "unit_price": p, **({"note": n} if n else {})} for i, q, p, n in s.lines],
        "subtotal": s.subtotal, "tax_rate": s.tax_rate, "tax_amount": s.tax, "total": s.total,
        "currency": s.currency, "payment_terms": "Net 30", **({"notes": s.notes} if s.notes else {}),
    }, indent=2)


def render_csv(s: Spec) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Invoice Number", "Vendor", "Date", "Due Date", "Item", "Qty", "Unit Price", "Line Total"])
    for item, qty, price, _ in s.lines:
        w.writerow([s.number, s.vendor, s.issued.isoformat(), s.due.isoformat() if s.due else "", item, qty, f"{price:.2f}", f"{qty * price:.2f}"])
    w.writerow(["", "", "", "", "", "", "Subtotal:", f"{s.subtotal:.2f}"])
    w.writerow(["", "", "", "", "", "", f"Tax ({s.tax_rate:.0%}):", f"{s.tax:.2f}"])
    w.writerow(["", "", "", "", "", "", "Total:", f"{s.total:.2f}"])
    return buf.getvalue()


def manifest_path(out_dir: Path) -> Path:
    return out_dir.parent / f"{out_dir.name}_manifest.json"


RENDERERS = {"txt": render_txt, "json": render_json, "csv": render_csv}


def _format_for(scenario: str, rng: random.Random) -> str:
    # CSV has no address/notes/currency columns, so scenarios that depend on those use TXT or JSON.
    if scenario in {"address_change", "foreign_currency"}:
        return "json"
    if scenario in {"unknown_vendor_pressure", "documented_premium"}:
        return rng.choice(["txt", "json"])
    return rng.choice(["txt", "json", "csv"])


def generate(n: int, out_dir: Path, seed: int = 7) -> dict[str, dict]:
    """Write ~n invoice files to out_dir and return the ground-truth manifest.

    The manifest is saved *next to* out_dir (``<out_dir>_manifest.json``), never inside it, so an
    inbox watcher or queue doesn't mistake it for an invoice.
    """
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    names, weights = [s[0] for s in SCENARIOS], [s[1] for s in SCENARIOS]
    expected = {name: outcomes for name, _, outcomes in SCENARIOS}
    manifest: dict[str, dict] = {}
    split_dates: dict[str, list[date]] = {}
    i = 0
    while len(manifest) < n:
        scenario = rng.choices(names, weights)[0]
        if scenario == "split_pair":
            # One ~$12K order billed as two ~$6K invoices, a day apart, to stay under the $10K VP threshold.
            vendor = rng.choice(VENDORS)
            issued = date(2026, 9, 1) + timedelta(days=rng.randint(0, 40))
            if any(abs((issued - d).days) < 7 for d in split_dates.get(vendor, [])):
                continue  # pick again: overlapping splits from one vendor would make the expected outcome ambiguous
            split_dates.setdefault(vendor, []).append(issued)
            halves = [[("WidgetA", 10, 250.0, None), ("WidgetB", 7, 500.0, None)], [("WidgetB", 6, 500.0, None), ("GadgetX", 4, 750.0, None)]]
            group = f"SPLIT-{i}"
            for part, lines in enumerate(halves):
                spec = Spec(number=f"INV-{50000 + i}{part}", vendor=vendor, issued=issued + timedelta(days=part),
                            due=issued + timedelta(days=30 + part), lines=lines, address=ADDRESSES.get(vendor))
                fmt = rng.choice(["txt", "json"])
                name = f"inv_{i:05d}_split{part}.{fmt}"
                (out_dir / name).write_text(RENDERERS[fmt](spec), encoding="utf-8")
                manifest[name] = {"scenario": scenario, "expected": ["paid", "pending_review"], "group": group, "rule": "one_paid_one_held"}
        elif scenario == "duplicate_pair":
            spec = _spec(rng, i, "clean")
            group = spec.number
            for fmt in rng.sample(["txt", "json", "csv"], 2):  # same invoice, two formats (like the PDF+TXT samples)
                name = f"inv_{i:05d}_{fmt}_dup.{fmt}"
                (out_dir / name).write_text(RENDERERS[fmt](spec), encoding="utf-8")
                manifest[name] = {"scenario": scenario, "expected": ["paid", "rejected"], "group": group, "rule": "one_paid_one_rejected"}
        else:
            spec = _spec(rng, i, scenario)
            fmt = _format_for(scenario, rng)
            name = f"inv_{i:05d}.{fmt}"
            (out_dir / name).write_text(RENDERERS[fmt](spec), encoding="utf-8")
            manifest[name] = {"scenario": scenario, "expected": sorted(expected[scenario]), "group": None,
                              "must_not_pay": "paid" not in expected[scenario]}
        i += 1
    manifest_path(out_dir).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic invoices with ground truth.")
    parser.add_argument("--n", type=int, default=1000)
    parser.add_argument("--out", type=Path, default=Path("data/synthetic"))
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    manifest = generate(args.n, args.out, args.seed)
    print(f"Wrote {len(manifest)} invoices to {args.out} (ground truth: {manifest_path(args.out)})")


if __name__ == "__main__":
    main()
