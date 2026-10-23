"""Turn an invoice file into (a) raw text and, for machine-readable formats, (b) a structured Invoice.

Structured formats (JSON / CSV / XML) are parsed deterministically - no reason to pay for an LLM
call (or risk a hallucination) when the data is already labelled. Free-text formats (TXT / PDF /
email) return raw text for the LLM extractor; `heuristic_extract` is the offline fallback for those.
"""

from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .models import Invoice, LineItem
from .normalize import fix_ocr_digits, parse_date, parse_money, parse_quantity

STRUCTURED_FORMATS = {"json", "csv", "xml"}


class UnsupportedDocument(ValueError):
    pass


@dataclass
class LoadedDocument:
    path: Path
    fmt: str  # json | csv | xml | txt | pdf
    text: str
    structured: Invoice | None = None
    notes: list[str] = field(default_factory=list)


def load_document(path: str | Path) -> LoadedDocument:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    fmt = path.suffix.lower().lstrip(".")
    if fmt == "pdf":
        return LoadedDocument(path, "pdf", _pdf_text(path))
    text = path.read_text(encoding="utf-8", errors="replace")
    if fmt == "json":
        return LoadedDocument(path, fmt, text, _from_json(json.loads(text)))
    if fmt == "csv":
        return LoadedDocument(path, fmt, text, _from_csv(text))
    if fmt == "xml":
        return LoadedDocument(path, fmt, text, _from_xml(text))
    if fmt in {"txt", "eml", "md", ""}:
        return LoadedDocument(path, "txt", text)
    raise UnsupportedDocument(f"Unsupported file type: .{fmt}")


def _pdf_text(path: Path) -> str:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    if not text.strip():
        raise UnsupportedDocument(f"{path.name} has no extractable text (scanned image? OCR not enabled)")
    return text


# ---- structured formats -------------------------------------------------------------------


def _from_json(data: dict) -> Invoice:
    vendor = data.get("vendor")
    vendor_name, vendor_address = (vendor.get("name"), vendor.get("address")) if isinstance(vendor, dict) else (vendor, None)
    items = [
        LineItem(
            item=str(li.get("item") or li.get("name") or li.get("description") or ""),
            quantity=parse_quantity(li.get("quantity", li.get("qty"))),
            unit_price=parse_money(li.get("unit_price", li.get("price"))),
            amount=parse_money(li.get("amount", li.get("line_total"))),
            note=li.get("note"),
        )
        for li in data.get("line_items") or data.get("items") or []
    ]
    return Invoice(
        invoice_number=data.get("invoice_number"),
        vendor=vendor_name or None,
        vendor_address=vendor_address,
        invoice_date=parse_date(data.get("date") or data.get("invoice_date")),
        due_date=parse_date(data.get("due_date")),
        due_date_raw=None if data.get("due_date") is None else str(data.get("due_date")),
        currency=(data.get("currency") or "USD").upper(),
        line_items=items,
        subtotal=parse_money(data.get("subtotal")),
        tax=parse_money(data.get("tax_amount", data.get("tax"))),
        tax_rate=data.get("tax_rate"),
        shipping=parse_money(data.get("shipping")),
        total=parse_money(data.get("total")),
        payment_terms=data.get("payment_terms") or None,
        notes=data.get("notes"),
        revision=data.get("revision"),
    )


def _from_csv(text: str) -> Invoice:
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        raise UnsupportedDocument("empty CSV")
    header = [h.strip().lower() for h in rows[0]]
    if header[:2] == ["field", "value"]:
        return _from_kv_csv(rows[1:])
    return _from_tabular_csv(header, rows[1:])


def _from_kv_csv(rows: list[list[str]]) -> Invoice:
    """`field,value` layout where item/quantity/unit_price repeat once per line item."""
    fields: dict[str, str] = {}
    items: list[dict] = []
    for key, value, *_ in rows:
        key = key.strip().lower()
        if key == "item":
            items.append({"item": value})
        elif key in {"quantity", "unit_price", "amount"} and items:
            items[-1][key] = value
        else:
            fields[key] = value
    return Invoice(
        invoice_number=fields.get("invoice_number"),
        vendor=fields.get("vendor"),
        invoice_date=parse_date(fields.get("date")),
        due_date=parse_date(fields.get("due_date")),
        due_date_raw=fields.get("due_date"),
        currency=(fields.get("currency") or "USD").upper(),
        line_items=[
            LineItem(item=i["item"], quantity=parse_quantity(i.get("quantity")), unit_price=parse_money(i.get("unit_price")),
                     amount=parse_money(i.get("amount")))
            for i in items
        ],
        subtotal=parse_money(fields.get("subtotal")),
        tax=parse_money(fields.get("tax")),
        total=parse_money(fields.get("total")),
        payment_terms=fields.get("payment_terms"),
    )


def _from_tabular_csv(header: list[str], rows: list[list[str]]) -> Invoice:
    """One row per line item, header fields repeated; trailing 'Subtotal:/Tax/Total:' summary rows."""

    def col(*names: str) -> int | None:
        for n in names:
            if n in header:
                return header.index(n)
        return None

    c_num, c_vendor, c_date, c_due = col("invoice number"), col("vendor"), col("date"), col("due date")
    c_item, c_qty, c_price, c_amount = col("item"), col("qty", "quantity"), col("unit price"), col("line total", "amount")
    inv = Invoice()
    for row in rows:
        row = row + [""] * (len(header) - len(row))
        if c_item is not None and row[c_item].strip():
            inv.invoice_number = inv.invoice_number or row[c_num]
            inv.vendor = inv.vendor or row[c_vendor]
            inv.invoice_date = inv.invoice_date or parse_date(row[c_date])
            if inv.due_date_raw is None:
                inv.due_date_raw, inv.due_date = row[c_due], parse_date(row[c_due])
            inv.line_items.append(
                LineItem(
                    item=row[c_item].strip(),
                    quantity=parse_quantity(row[c_qty]) if c_qty is not None else None,
                    unit_price=parse_money(row[c_price]) if c_price is not None else None,
                    amount=parse_money(row[c_amount]) if c_amount is not None else None,
                )
            )
            continue
        # summary row: label in some cell ending with ':' and the value in the next cell
        for i, cell in enumerate(row[:-1]):
            label = cell.strip().lower()
            if not label.endswith(":"):
                continue
            value = parse_money(row[i + 1])
            if label.startswith("subtotal"):
                inv.subtotal = value
            elif label.startswith("tax"):
                inv.tax = value
                rate = re.search(r"([\d.]+)\s*%", label)
                inv.tax_rate = float(rate.group(1)) / 100 if rate else None
            elif label.startswith("shipping"):
                inv.shipping = value
            elif "total" in label:
                inv.total = value
    return inv


def _from_xml(text: str) -> Invoice:
    root = ET.fromstring(text)

    def find(path: str) -> str | None:
        node = root.find(path)
        return node.text.strip() if node is not None and node.text else None

    items = [
        LineItem(
            item=(it.findtext("name") or it.findtext("item") or "").strip(),
            quantity=parse_quantity(it.findtext("quantity")),
            unit_price=parse_money(it.findtext("unit_price")),
            amount=parse_money(it.findtext("amount")),
        )
        for it in root.iter("item")
    ]
    tax_rate = find(".//tax_rate")
    return Invoice(
        invoice_number=find(".//invoice_number"),
        vendor=find(".//vendor"),
        invoice_date=parse_date(find(".//date")),
        due_date=parse_date(find(".//due_date")),
        due_date_raw=find(".//due_date"),
        currency=(find(".//currency") or "USD").upper(),
        line_items=items,
        subtotal=parse_money(find(".//subtotal")),
        tax=parse_money(find(".//tax_amount")),
        tax_rate=float(tax_rate) if tax_rate else None,
        shipping=parse_money(find(".//shipping")),
        total=parse_money(find(".//total")),
        payment_terms=find(".//payment_terms"),
        notes=find(".//notes"),
    )


# ---- free text: offline heuristic extractor -----------------------------------------------

_STOP = r"(?=\s{2,}|\s+(?:due(?:\s*(?:date|dt))?|date|dt|terms|vendor)\s*:|$)"
_MONEY = r"\(?-?\$?\s*-?[\d,]+(?:\.[\dOo]{1,2})?\)?"  # also '$-1,575.00'

_ITEM_LINE = re.compile(
    r"^\s*(?:[-*•]\s*)?"
    r"(?P<name>[A-Za-z][A-Za-z0-9\- ]*?(?:\s*\([^)]*\))?)\s+"
    r"(?:qty:?\s*|x(?=\d))?(?P<qty>-?\d+)\s+"
    r"(?:@\s*|unit\s+price:?\s*)?(?P<price>\$?\s*[\d,]+(?:\.[\dOo]{2})?)(?:\s*(?:ea|each)\b)?"
    r"(?:\s+(?P<amount>\$?\s*[\d,]+\.[\dOo]{2}))?"
    r"(?P<note>\s+[A-Za-z].*)?\s*$",
    re.I,
)


def _label(text: str, pattern: str) -> str | None:
    match = re.search(rf"(?im)(?:^|\s)(?:{pattern})\s*:\s*(.+?){_STOP}", text)
    return match.group(1).strip() if match else None


def heuristic_extract(text: str) -> Invoice:
    """Regex-based extraction for free-text invoices. Used when no LLM is available."""
    clean = fix_ocr_digits(text)
    inv = Invoice()

    num = re.search(r"(?i)\b(?:invoice|inv)\b\s*(?:number|no\.?|#)?\s*[:#]?\s*(INV[-\s]?\d+|\d{3,})", clean)
    inv.invoice_number = num.group(1) if num else None
    inv.vendor = _label(clean, r"vendor|vndr|from")
    if inv.vendor and "@" in inv.vendor:  # an email 'From:' header is not the vendor name
        inv.vendor = _label(re.sub(r"(?im)^from:.*$", "", clean), r"vendor|vndr|from")

    date_lines = [ln for ln in clean.splitlines() if not re.match(r"(?i)\s*due", ln)]
    inv.invoice_date = parse_date(_label("\n".join(date_lines), r"(?<!due )(?:invoice\s+)?(?:date|dt)"))
    inv.due_date_raw = _label(clean, r"due(?:\s*(?:date|dt))?")
    inv.due_date = parse_date(inv.due_date_raw)

    for line in clean.splitlines():
        m = _ITEM_LINE.match(line)
        if not m or re.search(r"(?i)total|tax|shipping|amount due|terms", m.group("name")):
            continue
        name = m.group("name").strip()
        paren = re.search(r"\(([^)]*)\)", name)
        note = (m.group("note") or "").strip() or (paren.group(1) if paren else None)
        inv.line_items.append(
            LineItem(item=name, quantity=parse_quantity(m.group("qty")), unit_price=parse_money(m.group("price")),
                     amount=parse_money(m.group("amount")), note=note)
        )

    def money_after(pattern: str) -> float | None:
        m = re.search(rf"(?im)(?:^|\s)(?:{pattern})[^:\n]*:\s*({_MONEY})", clean)
        return parse_money(m.group(1)) if m else None

    inv.subtotal = money_after(r"sub\s*-?\s*total")
    inv.tax = money_after(r"(?:sales\s+)?tax")
    rate = re.search(r"(?i)tax\s*\(\s*([\d.]+)\s*%\s*\)", clean)
    inv.tax_rate = float(rate.group(1)) / 100 if rate else None
    inv.shipping = money_after(r"shipping|freight")
    inv.total = money_after(r"(?<!sub)(?:grand\s+)?total(?:\s+amount)?|amt|amount\s+due")
    inv.payment_terms = _label(clean, r"(?:payment|pymnt)\s+terms|terms")
    notes = re.search(r"(?is)\bnotes?\s*:\s*(.+?)(?:\n\s*\n|\n\s*terms\s*:|$)", clean)
    inv.notes = re.sub(r"\s+", " ", notes.group(1)).strip() if notes else None
    if re.search(r"€|\bEUR\b", clean):
        inv.currency = "EUR"
    return inv
