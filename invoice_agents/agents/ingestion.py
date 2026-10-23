"""Ingestion Agent: document -> canonical Invoice, with a self-correction loop for LLM extraction."""

from __future__ import annotations

import json
import logging
import re
import time

from ..db import normalize_sku
from ..llm import LLMError
from ..models import Invoice
from ..normalize import fix_ocr_digits, normalize_invoice_number, parse_date
from ..parsers import heuristic_extract, load_document
from .. import prompts
from ..state import AgentContext, InvoiceState, trace_event

log = logging.getLogger(__name__)


def load_node(ctx: AgentContext):
    def load(state: InvoiceState) -> InvoiceState:
        started = time.perf_counter()
        try:
            doc = load_document(state["invoice_path"])
        except Exception as exc:
            return {"error": f"Could not read document: {exc}", "trace": [trace_event("ingestion", "load_failed", started, error=str(exc))]}
        update: InvoiceState = {
            "source_format": doc.fmt,
            "raw_text": doc.text,
            "extraction_attempts": 0,
            "extraction_feedback": [],
            "trace": [trace_event("ingestion", "document_loaded", started, format=doc.fmt, chars=len(doc.text))],
        }
        if doc.structured is not None:
            update["invoice"] = finalize(doc.structured).model_dump()
            update["extraction_method"] = "parser"
        return update

    return load


def extract_node(ctx: AgentContext):
    def extract(state: InvoiceState) -> InvoiceState:
        started = time.perf_counter()
        attempt = state.get("extraction_attempts", 0) + 1
        text = state["raw_text"]
        method, fallback_reason = "llm", None
        # Confidence routing: once the primary model has used its attempts, a configured second model
        # gets one fresh look before the invoice is handed to a human as unverified.
        llm = ctx.secondary_llm if (ctx.secondary_llm and attempt > ctx.settings.max_extraction_attempts) else ctx.llm
        if llm.is_offline:
            method = "heuristic"
        else:
            user = f"DOCUMENT ({state['source_format']}):\n<document>\n{text}\n</document>"
            if state.get("extraction_feedback"):
                user += "\n\n" + prompts.EXTRACTION_RETRY.format(
                    problems="\n".join(f"- {p}" for p in state["extraction_feedback"]),
                    previous=json.dumps(state.get("invoice"), indent=1),
                )
            try:
                invoice = llm.structured(system=prompts.EXTRACTOR, user=user, schema=Invoice)
            except LLMError as exc:
                log.warning("LLM extraction failed, using heuristic parser: %s", exc)
                method, fallback_reason = "heuristic", str(exc)
        if method == "heuristic":
            invoice = heuristic_extract(text)
        invoice = finalize(invoice)
        detail = {"attempt": attempt, "method": method, "model": llm.label if method == "llm" else None,
                  "second_opinion": llm is not ctx.llm}
        if fallback_reason:
            detail["fallback_reason"] = fallback_reason
        return {
            "invoice": invoice.model_dump(),
            "extraction_method": method,
            "extraction_attempts": attempt,
            "trace": [trace_event("ingestion", "extracted", started, **detail)],
        }

    return extract


def qa_node(ctx: AgentContext):
    """Self-check the extraction against the source text. Problems feed the retry loop."""

    def qa(state: InvoiceState) -> InvoiceState:
        started = time.perf_counter()
        problems = extraction_problems(Invoice(**state["invoice"]), state["raw_text"])
        return {
            "extraction_feedback": problems,
            "extraction_confidence": extraction_confidence(state.get("extraction_method"), state.get("extraction_attempts", 1), problems),
            "trace": [trace_event("ingestion", "self_check", started, ok=not problems, problems=problems,
                                  attempt=state.get("extraction_attempts", 0))],
        }

    return qa


def extraction_confidence(method: str | None, attempts: int, problems: list[str]) -> float:
    """How much to trust the extracted fields. Deterministic parsing of a structured file is ground truth;
    each retry and each unresolved self-check problem lowers confidence."""
    if method == "parser":
        return 1.0
    base = 0.9 if method == "heuristic" else 1.0
    return round(max(0.0, base - 0.1 * max(0, attempts - 1) - 0.3 * len(problems)), 2)


def finalize(inv: Invoice) -> Invoice:
    """Deterministic clean-up applied to every extraction, whichever path produced it."""
    inv = inv.model_copy(deep=True)
    inv.invoice_number = normalize_invoice_number(inv.invoice_number)
    vendor = (inv.vendor or "").strip()
    aside = re.search(r"\s*\(([^)]*)\)\s*$", vendor)  # 'QuickShip (formerly FastShip Ltd.)' -> name + note
    if aside:
        vendor = vendor[: aside.start()].strip()
        inv.notes = "; ".join(filter(None, [inv.notes, f"Vendor: {aside.group(1)}"]))
    inv.vendor = vendor or None
    for name in ("invoice_date", "due_date"):
        value = getattr(inv, name)
        if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            setattr(inv, name, parse_date(value))
    if inv.due_date is None and inv.due_date_raw:  # model put a real date only in the raw field
        inv.due_date = parse_date(inv.due_date_raw)
    if inv.due_date and not inv.due_date_raw:
        inv.due_date_raw = inv.due_date
    inv.currency = (inv.currency or "USD").upper().replace("$", "USD")
    if inv.tax_rate and inv.tax_rate > 1:  # model returned 5 instead of 0.05
        inv.tax_rate = inv.tax_rate / 100
    return inv


def _number_in_text(value: float, text: str) -> bool:
    variants = {f"{value:,.2f}", f"{value:.2f}", f"{value:,.0f}", f"{value:.0f}", f"{value:g}"}
    return any(v in text for v in variants)


def extraction_problems(inv: Invoice, raw_text: str) -> list[str]:
    """Is the extraction faithful to the document? (Not: is the invoice itself valid - that's validation's job.)"""
    text = fix_ocr_digits(raw_text)
    flat = normalize_sku(text)
    problems = []
    if not inv.vendor:
        problems.append("vendor is empty - look for the issuing company's name")
    elif re.match(r"(?i)acme\s*(corp|corporation)\b", inv.vendor):  # the customer - not 'Acme Industrial Supplies'
        problems.append("vendor is Acme Corp, which is the customer - the vendor is the company that issued the invoice")
    if not inv.line_items:
        problems.append("no line items extracted")
    if inv.total is None:
        problems.append("total is missing")
    elif not _number_in_text(abs(inv.total), text):
        problems.append(f"total {inv.total} does not appear anywhere in the document")
    if inv.due_date is None and not inv.due_date_raw:
        problems.append("due_date and due_date_raw are both empty - copy whatever the document says about the due date")
    for n, li in enumerate(inv.line_items, start=1):
        if normalize_sku(li.item) and normalize_sku(li.item) not in flat:
            problems.append(f"line {n}: item '{li.item}' does not appear in the document")
        if li.quantity is not None and not re.search(rf"(?<![\d.]){abs(li.quantity):g}(?![\d])", text):
            problems.append(f"line {n}: quantity {li.quantity:g} does not appear in the document")
    if inv.line_items and all(li.quantity is not None and li.unit_price is not None for li in inv.line_items):
        line_sum = sum(li.amount if li.amount is not None else li.quantity * li.unit_price for li in inv.line_items)
        if inv.subtotal is None and inv.total is not None and line_sum > inv.total + 0.01 and not inv.tax:
            problems.append(f"line items sum to {line_sum:,.2f}, more than the total {inv.total:,.2f} - check for a misread line")
    return problems


def should_retry_extraction(ctx: AgentContext):
    def route(state: InvoiceState) -> str:
        if state.get("error"):
            return "failed"
        limit = ctx.settings.max_extraction_attempts + (1 if ctx.secondary_llm else 0)
        retry = (
            state.get("extraction_feedback")
            and state.get("extraction_method") == "llm"
            and state.get("extraction_attempts", 0) < limit
        )
        return "retry" if retry else "continue"

    return route
