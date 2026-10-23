"""Load test: generate N invoices with known correct outcomes, push them through the job queue, score.

    python -m invoice_agents.loadtest --n 1000 --workers 8                       # offline rules
    python -m invoice_agents.loadtest --n 40 --workers 2 --provider ollama       # real local model
    python -m invoice_agents.loadtest --n 200 --backend mcp                      # tools over MCP

Runs in a throwaway workspace (its own DB), so the real ledger is untouched. The headline safety
number is `wrongly_paid`: invoices that should never be paid but were. It must be 0.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

from .config import PROJECT_ROOT, load_settings
from .db import Store
from .graph import InvoicePipeline
from .jobqueue import JobQueue
from .synthetic import generate


def score(manifest: dict[str, dict], outcomes: dict[str, str]) -> dict:
    correct = wrongly_paid = 0
    by_scenario: dict[str, Counter] = defaultdict(Counter)
    groups: dict[str, list[str]] = defaultdict(list)
    misses = []
    for name, truth in manifest.items():
        got = outcomes.get(name, "missing")
        by_scenario[truth["scenario"]][got] += 1
        if truth["group"]:
            groups[truth["group"]].append(got)
            continue
        if got in truth["expected"]:
            correct += 1
        else:
            misses.append({"file": name, "scenario": truth["scenario"], "expected": truth["expected"], "got": got})
        if truth.get("must_not_pay") and got == "paid":
            wrongly_paid += 1
    rules = {t["group"]: t.get("rule", "one_paid_one_rejected") for t in manifest.values() if t["group"]}
    other = {"one_paid_one_rejected": "rejected", "one_paid_one_held": "pending_review"}
    dup_ok = sum(1 for g, outs in groups.items()
                 if outs.count("paid") == 1 and outs.count(other[rules[g]]) == len(outs) - 1)
    double_paid = sum(1 for outs in groups.values() if outs.count("paid") > 1)
    singles = sum(1 for t in manifest.values() if not t["group"])
    return {
        "decision_accuracy": round((correct + 2 * dup_ok) / len(manifest), 4) if manifest else 0,
        "single_invoices_correct": f"{correct}/{singles}",
        "paired_groups_correct": f"{dup_ok}/{len(groups)}",
        "wrongly_paid": wrongly_paid,
        "paired_groups_double_paid": double_paid,
        "by_scenario": {k: dict(v) for k, v in sorted(by_scenario.items())},
        "misses": misses[:25],
    }


def run(n: int, workers: int, provider: str | None, model: str | None, backend: str | None, fast_path: bool,
        seed: int, workdir: Path | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="acme-loadtest-") as tmp:
        root = Path(workdir or tmp)
        inbox = root / "inbox"
        manifest = generate(n, inbox, seed)
        base = load_settings(provider=provider, model=model, tools_backend=backend, db_path=root / "loadtest.db")
        settings = replace(base, log_dir=root / "logs", workers=workers, fast_path=fast_path)
        Store(settings.db_path).init(reset=True)

        with contextlib.redirect_stdout(io.StringIO()), InvoicePipeline(settings) as pipeline:
            queue = JobQueue(pipeline.store)
            queue.enqueue_dir(inbox)
            started = time.perf_counter()
            queue.run_workers(pipeline, workers)
            seconds = time.perf_counter() - started
            rows = pipeline.store.list_invoices()
            llm_label = pipeline.llm.label
            model_seconds = pipeline.llm.usage.seconds

        by_file = {Path(r["source_file"]).name: r for r in rows}
        outcomes = {name: r["status"] for name, r in by_file.items()}
        durations = sorted(r["duration_ms"] or 0 for r in rows)
        calls = [r["llm_calls"] or 0 for r in rows]
        pct = lambda q: durations[min(len(durations) - 1, int(q * len(durations)))] if durations else 0  # noqa: E731
        report = {
            "invoices": len(manifest),
            "workers": workers,
            "llm": llm_label,
            "tools_backend": settings.tools_backend,
            "fast_path": fast_path,
            "wall_seconds": round(seconds, 2),
            "throughput_per_min": round(60 * len(rows) / seconds, 1) if seconds else None,
            "latency_ms": {"p50": pct(0.5), "p95": pct(0.95), "max": durations[-1] if durations else 0},
            "llm_calls_per_invoice": round(sum(calls) / len(calls), 2) if calls else 0,
            "invoices_needing_no_llm_call": sum(1 for c in calls if c == 0),
            "model_seconds": round(model_seconds, 1),
            "outcomes": dict(Counter(outcomes.values())),
            **score(manifest, outcomes),
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Load test the AP agents against synthetic invoices with ground truth.")
    parser.add_argument("--n", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--provider", default="offline")
    parser.add_argument("--model")
    parser.add_argument("--backend", choices=["local", "mcp"])
    parser.add_argument("--no-fast-path", action="store_true")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    report = run(args.n, args.workers, args.provider, args.model, args.backend, not args.no_fast_path, args.seed)
    out = PROJECT_ROOT / "logs" / f"loadtest_{report['llm'].replace(':', '-')}_{args.n}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary = {k: v for k, v in report.items() if k not in ("by_scenario", "misses")}
    print(json.dumps(summary, indent=2))
    if report["misses"]:
        print(f"{len(report['misses'])} miss(es), first few:")
        for m in report["misses"][:8]:
            print(f"  {m['file']:<22} {m['scenario']:<24} expected {m['expected']} got {m['got']}")
    print(f"full report: {out}")


if __name__ == "__main__":
    main()
