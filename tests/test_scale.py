"""Volume features: concurrency safety, the durable job queue, crash recovery, bank-side controls."""

import contextlib
import io
import shutil
from collections import Counter

from invoice_agents import bank
from invoice_agents.graph import InvoicePipeline
from invoice_agents.jobqueue import JobQueue

from .conftest import DATA
from .test_pipeline import EXPECTED


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def test_concurrent_batch_matches_sequential_outcome_counts(settings):
    with quiet(), InvoicePipeline(settings) as pipeline:
        batch = pipeline.process_many([DATA / n for n in EXPECTED], workers=8)
    counts = Counter(s["status"] for _, s in batch.results)
    assert counts == Counter(EXPECTED.values())
    paid = [p["invoice_number"] for p in pipeline.store.list_payments() if p["status"] == "success"]
    assert len(paid) == len(set(paid)), "an invoice number was paid twice"


def test_identical_copies_racing_are_paid_exactly_once(settings, tmp_path):
    for n in range(12):  # same invoice, different file names, all processed at once
        shutil.copy(DATA / "invoice_1001.txt", tmp_path / f"copy_{n}.txt")
    with quiet(), InvoicePipeline(settings) as pipeline:
        batch = pipeline.process_many(sorted(tmp_path.glob("*.txt")), workers=12)
    statuses = Counter(s["status"] for _, s in batch.results)
    assert statuses == {"paid": 1, "rejected": 11}
    assert len(pipeline.store.list_payments()) == 1


def test_per_invoice_llm_calls_and_timing_are_recorded(settings):
    with quiet(), InvoicePipeline(settings) as pipeline:
        state = pipeline.process(DATA / "invoice_1001.txt")
    row = pipeline.store.get_invoice(state["invoice_id"])
    assert row["llm_calls"] == 0 and row["duration_ms"] is not None


# ---- job queue --------------------------------------------------------------------------------


def test_queue_dedupes_intake_and_drains(settings):
    with quiet(), InvoicePipeline(settings) as pipeline:
        queue = JobQueue(pipeline.store)
        added, skipped = queue.enqueue_dir(DATA)
        assert (added, skipped) == (20, 0)
        assert queue.enqueue_dir(DATA) == (0, 20)  # same content again: no-op
        queue.run_workers(pipeline, workers=4)
    stats = queue.stats()
    assert (stats.done, stats.failed, stats.outstanding) == (20, 0, 0)


def test_transient_failures_are_retried(settings, monkeypatch):
    with quiet(), InvoicePipeline(settings) as pipeline:
        queue = JobQueue(pipeline.store, backoff_s=0)
        queue.enqueue(DATA / "invoice_1001.txt")
        real, calls = pipeline.process, {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("model timed out")
            return real(*args, **kwargs)

        monkeypatch.setattr(pipeline, "process", flaky)
        queue.run_workers(pipeline, workers=1, poll_s=0.01)
    job = queue.list_jobs()[0]
    assert (job["status"], job["attempts"], job["outcome"]) == ("done", 2, "paid")


def test_permanent_failures_stop_after_max_attempts(settings, monkeypatch):
    with quiet(), InvoicePipeline(settings) as pipeline:
        queue = JobQueue(pipeline.store, max_attempts=2, backoff_s=0)
        queue.enqueue(DATA / "invoice_1001.txt")
        monkeypatch.setattr(pipeline, "process", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        queue.run_workers(pipeline, workers=1, poll_s=0.01)
    job = queue.list_jobs()[0]
    assert (job["status"], job["attempts"]) == ("failed", 2) and "boom" in job["error"]


def test_crash_recovery_finishes_interrupted_payment_and_requeues_the_rest(settings):
    with quiet(), InvoicePipeline(settings) as pipeline:
        store = pipeline.store
        queue = JobQueue(store)
        queue.enqueue(DATA / "invoice_1001.txt")
        queue.enqueue(DATA / "invoice_1006.csv")
        crashed_after_approval, crashed_early = queue.claim(), queue.claim()
        # Simulate a crash right after the ledger write but before the bank confirmed.
        state = pipeline.process(DATA / "invoice_1001.txt", run_id=crashed_after_approval["run_id"])
        with store.connect() as conn:
            conn.execute("DELETE FROM payments")
            conn.execute("UPDATE invoices SET status = 'approved' WHERE id = ?", (state["invoice_id"],))

        assert queue.recover(pipeline) == 2
        assert store.get_invoice(state["invoice_id"])["status"] == "paid"
        assert len(store.list_payments()) == 1
        jobs = {j["id"]: j for j in queue.list_jobs()}
        assert jobs[crashed_after_approval["id"]]["status"] == "done"
        assert jobs[crashed_early["id"]]["status"] == "queued"  # never reached the ledger: runs again
        queue.run_workers(pipeline, workers=1)
    assert queue.stats().done == 2


# ---- bank controls ------------------------------------------------------------------------------


def test_bank_is_idempotent_and_enforces_limit(store):
    with quiet():
        first = bank.pay(store, invoice_id=1, invoice_number="INV-1", vendor="V", amount=100, currency="USD", single_payment_limit=1000)
        again = bank.pay(store, invoice_id=1, invoice_number="INV-1", vendor="V", amount=100, currency="USD", single_payment_limit=1000)
        big = bank.pay(store, invoice_id=2, invoice_number="INV-2", vendor="V", amount=5000, currency="USD", single_payment_limit=1000)
    assert first["status"] == "success" and again["status"] == "skipped" and again["reference"] == first["reference"]
    assert big["status"] == "declined" and len(store.list_payments()) == 1


# ---- synthetic load test as a regression test -----------------------------------------------------


def test_synthetic_load_is_fully_correct(tmp_path):
    from invoice_agents.loadtest import run

    with quiet():
        report = run(n=150, workers=4, provider="offline", model=None, backend="local", fast_path=True, seed=11, workdir=tmp_path)
    assert report["wrongly_paid"] == 0 and report["paired_groups_double_paid"] == 0
    assert report["decision_accuracy"] == 1.0, report["misses"]


def test_reset_then_reprocess_gives_identical_outcomes(settings):
    with quiet(), InvoicePipeline(settings) as pipeline:
        first = Counter(s["status"] for _, s in pipeline.process_many([DATA / n for n in EXPECTED], workers=4).results)
        pipeline.store.reset_ledger()
        assert pipeline.store.list_invoices() == [] and pipeline.store.list_payments() == []
        second = Counter(s["status"] for _, s in pipeline.process_many([DATA / n for n in EXPECTED], workers=4).results)
    assert first == second == Counter(EXPECTED.values())


def test_connections_from_finished_threads_are_closed(store):
    import threading

    for _ in range(25):  # like a UI that runs each page refresh on a new thread
        t = threading.Thread(target=store.list_inventory)
        t.start()
        t.join()
    store.list_inventory()
    assert store.open_connections() <= 2
