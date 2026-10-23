"""Small, pure helpers for cleaning messy values (OCR noise, mixed formats). Easy to unit-test."""

from __future__ import annotations

import re
from datetime import date

from dateutil import parser as dateparser

_RELATIVE_WORDS = {"yesterday", "today", "tomorrow", "asap", "immediately", "now", "upon receipt", "on receipt"}


def fix_ocr_digits(text: str) -> str:
    """Replace letter-O / lowercase-l used in place of 0 / 1 when they sit inside a number: '2O26' -> '2026'."""
    text = re.sub(r"(?<=\d)[Oo](?=[\d.,]|\b)|(?<=[\d.,])[Oo](?=\d)", "0", text)
    text = re.sub(r"(?<=\d)[Oo]", "0", text)  # repeated pass catches '.O0' -> '.00'
    text = re.sub(r"(?<=\d)[lI](?=\d)", "1", text)
    return text


def parse_money(value) -> float | None:
    """'$3,500.O0' -> 3500.0, '(250.00)' -> -250.0, None/'' -> None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = fix_ocr_digits(str(value)).strip()
    # '-$250', '$-250', '(250)' are all negative
    negative = (text.startswith("(") and text.endswith(")")) or bool(re.match(r"^[A-Z$€£\s]*-", text))
    digits = re.sub(r"[^\d.]", "", text)
    if not digits or digits == ".":
        return None
    try:
        amount = float(digits)
    except ValueError:
        return None
    return -amount if negative else amount


def parse_quantity(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = fix_ocr_digits(str(value)).strip().lower().lstrip("x").strip()
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def parse_date(value) -> str | None:
    """Return YYYY-MM-DD, or None if the value is missing, relative ('yesterday') or unparseable."""
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    text = fix_ocr_digits(str(value)).strip()
    if not text or text.lower() in _RELATIVE_WORDS:
        return None
    if not re.search(r"\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", text):
        return None  # require something year-like; avoids dateutil inventing dates from 'Net 30'
    try:
        return dateparser.parse(text, dayfirst=False, fuzzy=True).date().isoformat()
    except (ValueError, OverflowError):
        return None


def normalize_invoice_number(value) -> str | None:
    """'1002', 'INV 1012', 'inv-1003' -> 'INV-1002', 'INV-1012', 'INV-1003'. Other schemes keep every
    character that distinguishes them: '2026-0001' and '2026-0002' must stay different invoices."""
    if value is None:
        return None
    text = fix_ocr_digits(str(value)).strip().upper()
    if not text:
        return None
    if re.fullmatch(r"\d+", text):
        return f"INV-{text}"
    match = re.fullmatch(r"INV(?:OICE)?[\s#:._-]*(?:NO\.?|NUMBER)?[\s#:._-]*(\w[\w-]*)", text)
    if match:
        return f"INV-{match.group(1)}"
    return re.sub(r"[\s#:._]+", "-", text).strip("-")


def parse_terms_days(terms: str | None) -> int | None:
    """'Net 30' -> 30, 'Immediate' -> 0."""
    if not terms:
        return None
    match = re.search(r"net\s*(\d+)", terms, re.I)
    if match:
        return int(match.group(1))
    if re.search(r"immediate|due on receipt|upon receipt", terms, re.I):
        return 0
    return None
