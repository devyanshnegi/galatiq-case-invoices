# Command reference

The commands of `python main.py`. Run `python main.py COMMAND --help` for the same information in the terminal.

`INVOICE` can be the invoice number (`INV-1006`, `1006`) or the ID shown by `pending`. If the same invoice was received more than once (for example as a PDF and a TXT), `approve` and `reject` pick the copy that is waiting for review; otherwise you're asked to use the ID.

## Processing invoices

| Command | What it does |
|---|---|
| `python main.py --invoice_path=FILE` | The brief's command: processes one invoice with a live trace of each agent step |
| `python main.py process FILE` | The same thing |
| `python main.py batch` | Processes every invoice in `data/invoices` and prints a summary table |
| `python main.py batch FOLDER` | Processes every invoice in another folder |

Before processing, `batch` and `process` finish any payment an earlier crash left unconfirmed.

## Human review

Only people on the approver roster in `config/ap_policy.yaml` can review. **The names are placeholders**; a real deployment would take them from SSO or the HR system.

| Name (placeholder) | Role | Can approve up to |
|---|---|---|
| Dana Lee | VP Finance | $50,000 |
| Raj Mehta | VP Finance | $50,000 |
| Sam Patel | CFO | no limit |
| Ann Kim | AP specialist | $10,000 |
| Bo Chen | Department approver | $10,000 |

Give your name with `--as "Dana Lee"`, or set it once for the terminal session:

```powershell
$env:AP_REVIEWER="Dana Lee"        # PowerShell
set AP_REVIEWER=Dana Lee            # Command Prompt
export AP_REVIEWER="Dana Lee"       # macOS / Linux
```

| Command | What it does |
|---|---|
| `python main.py review` | Walks the review queue one invoice at a time. Keys: **a** approve, **r** reject, **h** hold, **d** details, **s** skip, **q** quit. |
| `python main.py pending` | Lists invoices waiting for a person, with approval progress ("1 of 2") and what is still needed |
| `python main.py show INVOICE` | Everything about one invoice: findings, decision, approvals, payment and the agent trace |
| `python main.py approve INVOICE --note "..."` | Approves an invoice. It is paid as soon as enough approvals are in. |
| `python main.py reject INVOICE --note "..."` | Rejects an invoice; the reason is required |

Invoices of $50,000 or more and bank or remit-address changes need two different people: the first approval is recorded as "1 of 2", and the second completes it and pays. Reopening a rejected invoice is done from the dashboard.

## Payments, ledger and setup

| Command | What it does |
|---|---|
| `python main.py payments` | Lists every payment and who authorised it |
| `python main.py reset` | Clears the ledger: processed invoices, payments, reviews and the audit log. Inventory and vendors are kept. Asks for confirmation. |
| `python main.py doctor` | Checks the policy file, the database, the model (with one real call) and unsettled payments, and prints a fix for anything wrong |

To start over: `python main.py reset --yes` then `python main.py batch`.

## Options

| Option | Works with | What it does |
|---|---|---|
| `--as NAME` | `review`, `approve`, `reject` | Who is acting; must be on the roster (default: `AP_REVIEWER`) |
| `--note TEXT` | `approve`, `reject` | The note or reason recorded with the action |
| `--provider NAME` | `process`, `batch`, `doctor` | Which LLM to use for this run: `ollama`, `xai`, `anthropic`, `openai` or `offline` (only `ollama` and `offline` have been tested) |
| `--dry-run` | `process`, `batch` | Runs on a throwaway copy of the ledger: shows what would happen, including what would be paid, without writing or paying anything |
| `--workers N` | `batch` | Number of invoices processed at the same time |
| `-q`, `--quiet` | `batch` | Summary only |
| `--yes`, `-y` | `reset` | Skip the confirmation |

## Settings in `.env`

Copy `.env.example` to `.env`:

| Setting | What it does |
|---|---|
| `LLM_PROVIDER` | `ollama` (default), `xai`, `anthropic`, `openai` or `offline` |
| `LLM_MODEL` | Model name, e.g. `qwen2.5:7b` or `grok-3` (blank = the provider's default) |
| `XAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | API keys for hosted models. These providers have not been tested against the real APIs. |
| `AP_REVIEWER` | Your name, so you don't need `--as` |

Approval thresholds, limits and the reviewer roster are in `config/ap_policy.yaml`. Less common settings are listed in `.env.example`.

## Other tools

| Command | What it does |
|---|---|
| `streamlit run app.py` | The dashboard, including reopening rejected invoices |
| `pytest` | Runs the test suite |
