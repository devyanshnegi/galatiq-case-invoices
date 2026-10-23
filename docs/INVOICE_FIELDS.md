# Invoice fields

[`data/templates/invoice_template.json`](../data/templates/invoice_template.json) is a complete invoice, filled in with **placeholder values** (the vendor IDs, tax ID and bank details are fictional). It holds the fields the system checks today, plus the few that would add the most value next.

The template runs through the system today: `python main.py --invoice_path=data/templates/invoice_template.json` parses it, passes every check, and pays it.

**Status column**
- ✅ **checked today**: read by the parser and used in a validation.
- 🔜 **next**: in the template, not yet validated.

**Required column**
- **Yes**: the invoice is rejected without it.
- **Should**: missing it weakens checks.
- **Optional**: nice to have.

## Header

| Field | Required | Example | What it's used for | Status |
|---|---|---|---|---|
| `invoice_number` | Should | `INV-3001` | Duplicate detection; revision detection (same number, different content) | ✅ |
| `invoice_type` | Optional | `standard`, `credit_note`, `proforma` | Credit notes reduce a balance rather than being paid; pro-formas must never be paid | 🔜 |
| `revision` / `supersedes_invoice_number` | Optional | `R1` / `INV-2990` | Links a revision to the original; only the difference is paid | ✅ partly |
| `invoice_date` | Should | `2026-10-20` | Due date can't precede it; terms check; split-invoice window | ✅ |
| `due_date` | **Yes** | `2026-11-19` | Payment scheduling; terms check; missing or invalid means rejection | ✅ |
| `currency` | Should | `USD` | Foreign currency goes to a human; catalog prices are compared only in USD | ✅ |

## Vendor and buyer

| Field | Required | Example | What it's used for | Status |
|---|---|---|---|---|
| `vendor.name` | **Yes** | `Widgets Inc.` | Vendor master match: exact, alias, or look-alike (escalate) | ✅ |
| `vendor.address` | Should | `100 Main St, Chicago, IL 60601` | A changed address is escalated | ✅ |
| `vendor.remit_to` | Should | name + address | **A remit-to address that differs from the vendor master is the top payment-redirection signal** | ✅ |
| `vendor.vendor_id` | Should | `V-000117` | Exact vendor match; a name/ID mismatch signals impersonation | 🔜 |
| `vendor.tax_id` | Should | `36-4815162` | Must match the vendor master; a different tax ID under a known name is a classic fraud pattern | 🔜 |
| `buyer.name` | Should | `Acme Corp` | The invoice is actually addressed to Acme | 🔜 |

## References

| Field | Required | Example | What it's used for | Status |
|---|---|---|---|---|
| `references.purchase_order_number` | **Should: the most valuable field after the total** | `PO-20261001` | **Three-way match**: invoice vs. purchase order vs. goods receipt. Blocks invoices for things never ordered. | 🔜 |
| `references.goods_receipt_number` | Should | `GRN-77310` | Bill only for what was actually received | 🔜 |

## Line items

| Field | Required | Example | What it's used for | Status |
|---|---|---|---|---|
| `item` | **Yes** | `WidgetA` | Catalog match; an unknown item means rejection | ✅ |
| `description` | Optional | `Standard widget, type A` | Fallback for item matching | ✅ fallback |
| `quantity` | **Yes** | `10` | Positive; within stock (added up across lines) | ✅ |
| `unit_price` | **Yes** (or `amount`) | `250.00` | Price vs. catalog (±10%) | ✅ |
| `discount_amount` | Optional | `100.00` | Line maths includes the discount; it can't exceed the line value (`discount_percent` is also accepted) | ✅ |
| `amount` | Should | `1900.00` | quantity × price − discount = amount | ✅ |
| `note` | Optional | `5% volume discount agreed on the PO` | Justifies a price difference (a documented rush premium is approved) | ✅ |
| `po_line_number` | Should with a PO | `2` | Line-level three-way match | 🔜 |
| `vendor_part_number` | Optional | `WI-WB-200` | Exact SKU match instead of matching by name | 🔜 |
| `unit_of_measure` | Should | `EA`, `BOX` | Catches billing per box at a per-each price, a common overcharge | 🔜 |

## Totals and tax

| Field | Required | Example | What it's used for | Status |
|---|---|---|---|---|
| `subtotal` | Should | `5900.00` | Sum of line amounts | ✅ |
| `shipping` | Optional | `150.00` | Included in the total; freight above 5% of goods is flagged | ✅ |
| `tax_rate`, `tax_amount` | Should | `0.0825`, `486.75` | Tax = rate × subtotal; a rate above 15% goes to a human | ✅ |
| `total` | **Yes** | `6536.75` | Subtotal + tax + shipping; must be positive | ✅ |
| `amount_paid`, `balance_due` | Optional | `0.00`, `6536.75` | **Pay `balance_due`, not `total`**, so deposits aren't paid twice | 🔜 |

## Payment

| Field | Required | Example | What it's used for | Status |
|---|---|---|---|---|
| `payment_terms` | Should | `2/10 Net 30` | Terms vs. due date (`TERMS_MISMATCH`) | ✅ |
| `payment.early_payment_discount_*` | Optional | 2% within 10 days | Pay early when the discount is worth it | 🔜 |
| `payment.accepted_methods` | Optional | `ACH`, `check` | Wire-only or crypto-only demands are fraud signals | ✅ via fraud keywords |
| `payment.bank` | Should | bank, holder, last 4, routing | **Must match the bank details on file**; a change is escalated and verified by phone | ✅ text signal / 🔜 structured |
| `notes` | Optional | free text | Fraud keywords, prompt-injection detection, LLM risk review | ✅ |

## Where to start

The 🔜 fields above, in order of value:

1. **Three-way match.** Add purchase-order and goods-receipt tables, and check `purchase_order_number` and `po_line_number` against them. This blocks the biggest class of AP loss: paying for things never ordered or never received.
2. **Structured bank verification.** Store each vendor's bank details in the vendor master and compare them with `payment.bank`, not just keywords.
3. **Vendor ID and tax ID match.** Cheap to add, and very effective against impersonation.
4. **Pay `balance_due` on the best date**, taking early-payment discounts when they're worth it.
