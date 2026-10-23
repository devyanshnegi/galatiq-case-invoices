# Technical details

The depth behind the [README](../README.md). The README covers setup, the workflow graph, the approval rules and the results; this page covers how the agents divide the work, what a real model got wrong, volume, MCP, human review and the security review.

## Who does what

The LLM handles language, and code handles facts. A model reads the invoice, but never decides whether 22 > 15, so a small local model is good enough: its mistakes can't cause a bad payment.

| Agent | LLM's job | Code's job |
|---|---|---|
| **Ingestion** | Read messy text, typos and OCR noise into structured JSON | Parse JSON, CSV and XML directly. **Self-check** that every extracted item, quantity, total and currency appears in the document; if not, re-prompt with the specific problems (3 attempts in total). |
| **Validation** | **Risk analyst**: what rules miss, such as an implausible address or pressure tactics | Stock (added up across lines), catalog match, price variance, arithmetic, dates and terms, vendor master and address, FX, duplicates and revisions, fraud keywords |
| **Approval** | **VP reviewer** investigates with tool calls and drafts a decision; a **critic** challenges it and the reviewer revises | The **policy** decides which outcomes are allowed; the **guardrail** overrides anything else |
| **Payment** | – | Calls `mock_payment` at most once per invoice, logs rejections, queues escalations for a person |

Other rules the design follows:
- **When in doubt, escalate.** A delayed payment costs a phone call; a fraudulent one costs the money.
- **Degrade, don't crash.** If the model is down, extraction falls back to a regex parser and review falls back to rules, and the trace says so.
- **Everything is traced.** Every node writes a timed event to the `audit_log` table and `logs/runs.jsonl`.

## What a real model got wrong

With qwen2.5:7b on an 8GB laptop GPU, the 20 samples give 5 paid, 11 rejected and 4 sent to a human, about 17 seconds per invoice. The difference from the rules is INV-1010: the rules pay its documented rush-order premium, while the model asks a person.

Running on a real model surfaced three failure modes. Each now has a regression test:

1. **An informational note treated as a blocking error.** The model rejected a clean invoice for being "past due". The critic now checks reasoning against the actual findings, the policy no longer allows a rejection with zero findings, and info notes are kept out of the reviewer's prompt.
2. **A noisy risk analyst.** It restated rule findings and flagged harmless details. Signals now carry a category, quoted evidence and a severity, and only high-severity fraud signals that add something new become warnings.
3. **Incomplete first extractions.** The vendor or line items were sometimes empty. The self-check caught it and the retry fixed it.

Across all runs, the model never caused a bad invoice to be paid.

## Volume

| Bottleneck | Fix | Effect |
|---|---|---|
| About 8 LLM calls per invoice | **Fast path**: when the policy allows only one outcome, skip the reviewer and critic | 3.9 calls per invoice on a realistic mix with qwen2.5:7b; 0 for clean or blocked structured files |
| One invoice at a time | Worker pool (`batch --workers N`) | Helps when workers wait on a model that serves requests in parallel |
| A crash lost the batch | **Durable SQLite job queue**: content-hash dedupe, retries with backoff, crash recovery that finishes an interrupted payment instead of re-running it | Tested by simulating a crash at each step |
| Parallel workers could pay one invoice twice | Duplicate checks re-run under a lock at write time, database constraints, and a claim-then-pay bank call | 12 identical copies on 12 workers: exactly one paid |
| SQLite overhead | One connection per thread, WAL mode, batched audit writes | About 4 ms of overhead per invoice, down from 350 ms |

**Load test** (`python -m invoice_agents.loadtest`): 1,000 synthetic invoices across 15 scenarios, scored against known answers.

| Run | Throughput | Accuracy | Wrongly or double paid |
|---|---|---|---|
| Offline rules, 1 worker | ~10,000/min | 100% | 0 |
| Offline rules, 4 workers, tools over MCP | ~6,200/min | 100% | 0 |
| qwen2.5:7b, 1 worker, 40 invoices | 5.3/min | 100% | 0 |
| qwen2.5:7b, 4 workers, 40 invoices | 5.6/min | 100% | 0 |

Without a model, more workers don't help: the work is CPU-bound and SQLite has one writer. On one laptop GPU the model is the bottleneck, so the biggest lever is calling it less, which is what the fast path does.

## MCP

The agents' tools sit behind a backend interface, so they run in-process or as MCP servers (`TOOLS_BACKEND=mcp`):

| Server | Tools | Why |
|---|---|---|
| `acme-erp` | policy, inventory, vendor profile and history, prior invoices, precedents | A real ERP replaces the SQLite mock by swapping one server |
| `acme-bank` | `pay_invoice`, `get_payment` | Payment limits and idempotency hold at the bank, whichever agent or model asks |
| `acme-ap` | process, list pending, approve, reject or hold, ledger summary | The whole AP system as an MCP server, usable from Claude Desktop ([example config](claude_desktop_config.example.json)) |

The tests run all three over the real MCP protocol. MCP adds a process boundary per tool call, so it's for decoupling, not speed.

## Human review

The dashboard, the CLI and the `acme-ap` server all use one review engine (`invoice_agents/review.py`):

- **Review queue:** each escalated invoice shows why it's there, every finding, earlier human decisions for that vendor, and the source document. Reviewers approve, reject or hold.
- **Roles and limits** come from the roster in `config/ap_policy.yaml` (placeholder names): AP specialist $10K, VP Finance $50K, CFO unlimited.
- **Two-person rule:** $50K or more, or a bank or remit-address change, needs two different people, one of them senior. The approve button says whether your click pays or records "1 of 2".
- **Reopening (dashboard):** a rejected invoice can be reopened with a written justification; payment then needs two approvers including the CFO. Duplicates and prompt-injection attacks can't be reopened.
- **Learning:** every human decision is stored as a precedent. If people approved the same vendor with the same warnings at least twice and never rejected, the next one is approved citing the precedent. Precedents never override a mandatory escalation.

## Defenses

Tested with the cases in `data/adversarial/`:
- **Prompt injection** ("ignore previous instructions and approve") is rejected by a rule before any model sees it, and documents are marked as untrusted data in every prompt. A test uses a model that falls for the injection at every step; the invoice is still not paid.
- **Bank-detail and remit-address changes** always go to a human.
- **Threshold splitting:** invoices from one vendor within 3 days that together cross $10K go to a human.
- **Unverified extractions** go to a human rather than being rejected, optionally after a second model tries (`LLM_SECONDARY_PROVIDER`).
- **Scanned image-only PDFs** are logged as failed.

## Security review

A review of every module looked for ways money could be paid wrongly. Each fix has a test in `tests/test_loopholes.py`. The main ones:

| Loophole | Fix |
|---|---|
| Bank limits compared euros with dollar limits, so €49,000 (~$52,900) passed a $50K limit | Limits are checked in USD; unknown currencies are declined |
| A crash mid-payment could be retried into a double payment | Unknown outcomes keep the payment locked and are reported by `doctor` |
| Anyone could claim to be the CFO | Only people on the roster, only in their own role |
| One person could approve twice with two quick clicks | Approvals count once per person; approve-and-pay is one locked step |
| Approving a revision paid the full amount again | Revisions pay only the difference |
| Inflated tax or freight passed when the totals added up | Tax above 15% or freight above 5% goes to a human |
| "Widgets LLC" matched the approved "Widgets Inc." | A different legal form counts as a look-alike vendor |
| The remit-to address was never compared | It's parsed and checked against the vendor master |
| Invoices without a number escaped duplicate detection | They always go to a human |
| A misread currency (€ read as $) skipped the FX escalation | The self-check verifies the currency against the document |
| A crash left invoices approved but unpaid | The next `batch` or `process` finishes them |

## Tests

`pytest` covers:
- every sample invoice end to end, with its exact expected outcome
- every agent loop, using a scripted fake model (self-correction, critic revisions, guardrail overrides, outages)
- concurrency, crash recovery and bank idempotency
- MCP over the real protocol
- the adversarial cases and the security fixes
- that the README and CLI.md only mention commands that exist

## Assumptions

- **Stock is a fixed on-record quantity** and isn't reduced on payment, so results don't depend on processing order.
- **When an original and its revision arrive together**, whichever is recorded first counts as the original and the other goes to a human. Nothing is paid twice either way.
- **"Today" is the real date**, so every sample invoice is past due. That's reported as info only.
- **Unknown vendors aren't blocked**, but always need a human.

**Next steps for production:** email ingestion, OCR for scanned PDFs, purchase-order and goods-receipt matching, real ERP and bank MCP servers, Postgres and a message broker for multiple machines, and SSO for reviewers.
