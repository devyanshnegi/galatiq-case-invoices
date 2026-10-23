"""Durable job queue on SQLite: intake dedupe, worker pool, retries with backoff, crash recovery.

    enqueue(file) ──> jobs[queued] ──claim──> jobs[running] ──> jobs[done | failed]
                          ^                        │ exception
                          └──── retry w/ backoff ──┘ (until max_attempts)

* The same file content is only ever queued once (sha256), so a re-sent email attachment is a no-op.
* Each job gets its run_id *before* processing. If the process dies mid-invoice, `recover()` checks
  whether that run already reached the ledger: if so the job is closed with that outcome (finishing
  an interrupted payment idempotently), otherwise it is re-queued. Nothing is processed twice.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .db import Store, now_iso
from .agents.payment import settle_unconfirmed
from .graph import InvoicePipeline

log = logging.getLogger(__name__)

INVOICE_SUFFIXES = {".txt", ".pdf", ".json", ".csv", ".xml", ".eml"}


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _later(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


@dataclass
class QueueStats:
    queued: int = 0
    running: int = 0
    done: int = 0
    failed: int = 0

    @property
    def outstanding(self) -> int:
        return self.queued + self.running


class JobQueue:
    def __init__(self, store: Store, max_attempts: int = 3, backoff_s: float = 2.0):
        self.store, self.max_attempts, self.backoff_s = store, max_attempts, backoff_s

    # ---- intake ----------------------------------------------------------------------------

    def enqueue(self, path: Path) -> int | None:
        """Queue a file. Returns the job id, or None if identical content was already queued."""
        digest = file_hash(path)
        with self.store.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO jobs (path, file_hash, status, available_at, enqueued_at) VALUES (?, ?, 'queued', ?, ?)",
                (str(path), digest, now_iso(), now_iso()),
            )
            return int(cur.lastrowid) if cur.rowcount else None

    def enqueue_dir(self, directory: Path) -> tuple[int, int]:
        """Queue every invoice file in a directory. Returns (newly queued, skipped as already seen)."""
        added = skipped = 0
        for path in sorted(directory.iterdir()):
            if path.is_file() and path.suffix.lower() in INVOICE_SUFFIXES:
                if self.enqueue(path) is None:
                    skipped += 1
                else:
                    added += 1
        return added, skipped

    # ---- work ------------------------------------------------------------------------------

    def claim(self) -> dict | None:
        """Atomically take the next available job (safe across threads and processes)."""
        run_id = uuid.uuid4().hex[:12]
        with self.store.connect() as conn:
            row = conn.execute(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1, started_at = ?, run_id = ? "
                "WHERE id = (SELECT id FROM jobs WHERE status = 'queued' AND available_at <= ? ORDER BY id LIMIT 1) "
                "RETURNING id, path, attempts, run_id",
                (now_iso(), run_id, now_iso()),
            ).fetchone()
        return dict(row) if row else None

    def complete(self, job_id: int, *, outcome: str, invoice_id: int | None) -> None:
        with self.store.connect() as conn:
            conn.execute("UPDATE jobs SET status = 'done', outcome = ?, invoice_id = ?, finished_at = ?, error = NULL WHERE id = ?",
                         (outcome, invoice_id, now_iso(), job_id))

    def fail(self, job_id: int, attempts: int, error: str) -> None:
        with self.store.connect() as conn:
            if attempts < self.max_attempts:
                conn.execute("UPDATE jobs SET status = 'queued', error = ?, available_at = ? WHERE id = ?",
                             (error, _later(self.backoff_s * 2 ** (attempts - 1)), job_id))
            else:
                conn.execute("UPDATE jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?", (error, now_iso(), job_id))

    def recover(self, pipeline: InvoicePipeline) -> int:
        """After a crash: settle jobs left 'running'. Returns how many were recovered."""
        with self.store.connect() as conn:
            stale = [dict(r) for r in conn.execute("SELECT id, run_id FROM jobs WHERE status = 'running'")]
        for job in stale:
            record = self.store.invoice_for_run(job["run_id"]) if job["run_id"] else None
            if record is None:
                with self.store.connect() as conn:  # never reached the ledger: just run it again
                    conn.execute("UPDATE jobs SET status = 'queued', available_at = ? WHERE id = ?", (now_iso(), job["id"]))
                continue
            if record["status"] == "approved":  # crashed between approval and the bank confirming: finish it
                settle_unconfirmed(self.store, pipeline.backend, older_than_s=0)
                record = self.store.get_invoice(record["id"])
            self.complete(job["id"], outcome=record["status"], invoice_id=record["id"])
        return len(stale)

    def stats(self) -> QueueStats:
        with self.store.connect() as conn:
            counts = dict(conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
        return QueueStats(**{k: counts.get(k, 0) for k in ("queued", "running", "done", "failed")})

    def list_jobs(self, limit: int = 200) -> list[dict]:
        with self.store.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))]

    # ---- workers ---------------------------------------------------------------------------

    def run_workers(self, pipeline: InvoicePipeline, workers: int, *, stop: threading.Event | None = None,
                    until_empty: bool = True, on_done: Callable[[dict, dict], None] | None = None, poll_s: float = 0.5) -> None:
        """Run `workers` threads pulling jobs until the queue drains (or `stop` is set)."""
        stop = stop or threading.Event()

        def worker() -> None:
            while not stop.is_set():
                job = self.claim()
                if job is None:
                    if until_empty and self.stats().outstanding == 0:
                        return
                    stop.wait(poll_s)
                    continue
                try:
                    state = pipeline.process(Path(job["path"]), run_id=job["run_id"])
                except Exception as exc:  # transient (DB lock, model timeout...) -> retry with backoff
                    log.warning("job %s attempt %s failed: %s", job["id"], job["attempts"], exc)
                    self.fail(job["id"], job["attempts"], f"{type(exc).__name__}: {exc}")
                    continue
                self.complete(job["id"], outcome=state.get("status", "failed"), invoice_id=state.get("invoice_id"))
                if on_done:
                    on_done(job, state)

        threads = [threading.Thread(target=worker, name=f"worker-{n}", daemon=True) for n in range(max(1, workers))]
        for t in threads:
            t.start()
        try:
            for t in threads:
                while t.is_alive():
                    t.join(0.5)
        except KeyboardInterrupt:
            stop.set()
            for t in threads:
                t.join()
            raise

    def watch(self, directory: Path, pipeline: InvoicePipeline, workers: int, *, interval_s: float = 2.0,
              stop: threading.Event | None = None, on_done: Callable[[dict, dict], None] | None = None,
              on_enqueue: Callable[[int], None] | None = None) -> None:
        """Watch a folder (e.g. where an email gateway drops attachments) and process new files as they land."""
        stop = stop or threading.Event()
        directory.mkdir(parents=True, exist_ok=True)

        def scan() -> None:
            while not stop.is_set():
                added, _ = self.enqueue_dir(directory)
                if added and on_enqueue:
                    on_enqueue(added)
                stop.wait(interval_s)

        scanner = threading.Thread(target=scan, name="watcher", daemon=True)
        scanner.start()
        try:
            self.run_workers(pipeline, workers, stop=stop, until_empty=False, on_done=on_done)
        finally:
            stop.set()
            scanner.join(interval_s + 1)
