"""End-to-end: the whole graph, offline, over every sample invoice in a realistic processing order."""

import contextlib
import io

from invoice_agents.agents.payment import resolve_pending
from invoice_agents.graph import InvoicePipeline

from .conftest import DATA

# Processed in filename order against one ledger, so duplicates/revisions are detected across files.
EXPECTED = {
    "invoice_1001.txt": "paid",
    "invoice_1002.txt": "rejected",           # stock
    "invoice_1003.txt": "rejected",           # fraud / zero-stock item / 'yesterday'
    "invoice_1004.json": "paid",
    "invoice_1004_revised.json": "pending_review",  # revision of an already-paid invoice
    "invoice_1005.json": "rejected",          # GadgetX over stock, unknown vendor
    "invoice_1006.csv": "paid",
    "invoice_1007.csv": "rejected",           # over stock + total doesn't add up
    "invoice_1008.txt": "rejected",           # unknown items
    "invoice_1009.json": "rejected",          # negative qty, no vendor, no due date
    "invoice_1010.txt": "paid",               # documented rush premium
    "invoice_1011.pdf": "paid",
    "invoice_1011.txt": "rejected",           # same invoice again -> duplicate
    "invoice_1012.pdf": "pending_review",     # look-alike vendor name
    "invoice_1012.txt": "rejected",           # duplicate of the pending one
    "invoice_1013.json": "rejected",          # aggregated stock + total mismatch
    "invoice_1013.pdf": "rejected",
    "invoice_1014.xml": "pending_review",     # EUR
    "invoice_1015.csv": "paid",
    "invoice_1016.json": "rejected",          # WidgetC unknown
}


def run_all(settings):
    pipeline = InvoicePipeline(settings)
    results = {}
    with contextlib.redirect_stdout(io.StringIO()):
        for name in EXPECTED:
            results[name] = pipeline.process(DATA / name)
    return pipeline, results


def test_every_sample_reaches_the_expected_outcome(settings):
    _, results = run_all(settings)
    assert {n: s["status"] for n, s in results.items()} == EXPECTED


def test_payments_only_for_approved_invoices(settings):
    pipeline, results = run_all(settings)
    paid = {p["invoice_number"] for p in pipeline.store.list_payments()}
    assert paid == {"INV-1001", "INV-1004", "INV-1006", "INV-1010", "INV-1011", "INV-1015"}
    for s in results.values():
        assert s["decision"]["reasoning"]  # every outcome is explained


def test_human_review_approves_and_pays_once(settings):
    pipeline, results = run_all(settings)
    invoice_id = results["invoice_1014.xml"]["invoice_id"]
    with contextlib.redirect_stdout(io.StringIO()):
        resolve_pending(pipeline.store, invoice_id, approve=True, reviewer="Dana Lee", note="FX confirmed")
    assert pipeline.store.get_invoice(invoice_id)["status"] == "paid"
    try:
        resolve_pending(pipeline.store, invoice_id, approve=True, reviewer="Dana Lee")
        raise AssertionError("second approval should fail")
    except ValueError:
        pass
    assert sum(1 for p in pipeline.store.list_payments() if p["invoice_number"] == "INV-1014") == 1


def test_unreadable_document_fails_gracefully(settings, tmp_path):
    bad = tmp_path / "scan.pdf"
    bad.write_bytes(b"not a pdf")
    state = InvoicePipeline(settings).process(bad)
    assert state["status"] == "failed" and "Could not read" in state["error"]


def test_trace_and_audit_log_written(settings):
    pipeline, results = run_all(settings)
    stages = {e["stage"] for e in results["invoice_1001.txt"]["trace"]}
    assert stages == {"ingestion", "validation", "approval", "payment"}
    assert pipeline.store.list_audit()
    assert (settings.log_dir / "runs.jsonl").exists()
