# The complete invoice: field reference

[`data/templates/invoice_template.json`](../data/templates/invoice_template.json) is an invoice with every field a real invoice can carry, filled in. Real invoices carry a subset. **Every extra field a vendor provides is something more we can check.**

The template runs through the system today: `python main.py --invoice_path=data/templates/invoice_template.json` parses it, passes every check, and pays it.

**Status column**
- ✅ **checked today**: read by the parser and used in a validation.
- 🔜 **ready to use**: in the template, not yet validated. The "Validation it enables" column is the backlog, roughly in order of value.

**Required column**
- **Yes**: the invoice is rejected without it.
- **Should**: missing it weakens checks.
- **Optional**: nice to have.

## 1. Header

| Field | Required | Example | Validation it enables | Status |
|---|---|---|---|---|
| `invoice_number` | Should | `INV-3001` | Duplicate detection; revision detection (same number, different content) | ✅ |
| `invoice_type` | Optional | `standard`, `credit_note`, `debit_note`, `proforma`, `recurring`, `final` | Credit notes legitimately have negative totals and should reduce a balance, not be paid. Pro-formas must never be paid. | 🔜 |
| `revision` / `supersedes_invoice_number` | Optional | `R1` / `INV-2990` | Link a revision to the original explicitly instead of inferring it; pay only the difference | ✅ partly |
| `invoice_date` | Should | `2026-10-20` | Due date can't precede it; terms vs. due-date consistency; split-invoice window | ✅ |
| `tax_point_date` | Optional | `2026-10-18` | Which tax rate applies when rates change; must not be after the invoice date | 🔜 |
| `due_date` | **Yes** | `2026-11-19` | Payment scheduling; terms check; "yesterday" or missing means rejection | ✅ |
| `currency` | Should | `USD` | FX handling and treasury escalation; catalog price comparison only in the base currency | ✅ |
| `language`, `page_count` | Optional | `en-US`, `1` | `page_count` lets a missing page (truncated PDF) be detected | 🔜 |
| `e_invoice_id` | Optional | `US-EI-2026-0003001` | Global uniqueness from an e-invoicing network: the strongest duplicate key | 🔜 |

## 2. Vendor (seller)

| Field | Required | Example | Validation it enables | Status |
|---|---|---|---|---|
| `vendor.name` | **Yes** | `Widgets Inc.` | Vendor master match: exact, alias, or look-alike (escalate) | ✅ |
| `vendor.former_names` / `trading_name` | Optional | `["FastShip Ltd."]` | Alias matching without a human, as with "QuickShip (formerly FastShip)" | 🔜 |
| `vendor.vendor_id` | Should | `V-000117` | Exact vendor match by ID. Name/ID mismatch is a strong impersonation signal. | 🔜 |
| `vendor.tax_id` / `vat_id` | Should | `36-4815162` | Must match the vendor master. A different tax ID under a known name is a classic fraud pattern. | 🔜 |
| `vendor.company_registration_number`, `duns_number` | Optional | `IL-5521873` | KYC for new vendors; cross-check with the master | 🔜 |
| `vendor.address` | Should | `100 Main St, Chicago, IL 60601` | Changed address means escalation (`VENDOR_ADDRESS_MISMATCH`) | ✅ |
| `vendor.address_structured` | Optional | city, region, postal code, country | Country checks (sanctions, cross-border tax) without parsing free text | 🔜 |
| `vendor.remit_to` | Should | name + address | **Remit-to differing from the vendor master is the #1 payment-redirection signal** | ✅ |
| `vendor.contact` | Optional | name, email, phone | Email domain matches the vendor's known domain; who to call for out-of-band verification | 🔜 |

## 3. Buyer (Acme)

| Field | Required | Example | Validation it enables | Status |
|---|---|---|---|---|
| `buyer.name` | Should | `Acme Corp` | Invoice actually addressed to Acme, not a sister company or a scam blast | 🔜 |
| `buyer.customer_account_number` | Optional | `ACME-00042` | Must match the account number the vendor holds for Acme | 🔜 |
| `buyer.tax_id` | Optional | `74-1029384` | Required for some tax regimes; must be Acme's | 🔜 |
| `buyer.ship_to` | Optional | Dock B, Austin | Must be an Acme site; goods shipped elsewhere is a fraud signal | 🔜 |
| `buyer.department`, `cost_center`, `requester` | Optional | `CC-4100`, `Jim Ortega` | Route approval to the right budget owner; cost-center budget checks | 🔜 |

## 4. References (three-way match)

| Field | Required | Example | Validation it enables | Status |
|---|---|---|---|---|
| `references.purchase_order_number` | **Should: the most valuable field after the total** | `PO-20261001` | **Three-way match**: invoice vs. PO (items, quantities, agreed prices) vs. goods receipt. Blocks invoices for things never ordered. | 🔜 |
| `references.goods_receipt_number` / `delivery_note_number` / `packing_slip_number` | Should | `GRN-77310` | Bill only for what was actually received | 🔜 |
| `references.contract_number` / `quote_number` | Optional | `MSA-2025-018` | Contracted prices and discounts override catalog prices | 🔜 |
| `references.sales_order_number` | Optional | `SO-55120` | Vendor-side reference for disputes | 🔜 |
| `references.bill_of_lading`, `tracking_numbers` | Optional | `1Z999…` | Proof of shipment | 🔜 |
| `references.project_code` | Optional | `PRJ-LINE-3-UPGRADE` | Project budget tracking | 🔜 |

## 5. Delivery

| Field | Required | Example | Validation it enables | Status |
|---|---|---|---|---|
| `delivery.ship_date`, `delivery_date` | Optional | `2026-10-16`, `2026-10-18` | Delivery shouldn't be after the invoice date for goods; ship ≤ delivery | 🔜 |
| `delivery.incoterms` | Optional | `FOB Shipping Point` | Who pays freight. A freight charge under "Delivered Duty Paid" is an overcharge. | 🔜 |
| `delivery.carrier`, `shipping_method`, `ship_from` | Optional | `UPS Freight` | Freight charge reasonableness | 🔜 |

## 6. Line items

| Field | Required | Example | Validation it enables | Status |
|---|---|---|---|---|
| `item` | **Yes** | `WidgetA` | Catalog match; unknown item means rejection | ✅ |
| `buyer_part_number` / `vendor_part_number` | Should | `WidgetA` / `WI-WA-100` | Exact SKU match instead of name matching; cross-reference table per vendor | ✅ / 🔜 |
| `description` | Optional | `Standard widget, type A` | Fallback for item matching | ✅ fallback |
| `quantity` | **Yes** | `10` | Positive; within stock (aggregated across lines); within the PO quantity | ✅ |
| `unit_of_measure` | Should | `EA`, `BOX`, `KG` | **Unit mismatch** (billing per box at a per-each price) is a common overcharge | 🔜 |
| `unit_price` | **Yes** (or `amount`) | `250.00` | Price vs. catalog (±10%); vs. PO/contract price once those are linked. A line with neither a unit price nor an amount is rejected. | ✅ |
| `discount_amount` / `discount_percent` | Optional | `100.00` / `0.05` | Line maths includes the discount; discount can't exceed the line value | ✅ |
| `amount` | Should | `1900.00` | qty × price − discount = amount | ✅ |
| `tax_code`, `tax_rate`, `tax_amount` (per line) | Optional | `TX-STD`, `0.0825` | Per-line tax adds up to the tax summary; exempt items not taxed | 🔜 |
| `po_line_number` | Should with a PO | `2` | Line-level three-way match | 🔜 |
| `gl_account`, `cost_center` | Optional | `5100-RAW-MATERIALS` | Auto-coding for the ERP posting | 🔜 |
| `lot_numbers`, `serial_numbers` | Optional | `GX-26-00481` | Traceability; serial count equals quantity; a serial billed twice across invoices is a duplicate | 🔜 |
| `country_of_origin`, `hs_code` | Optional | `US`, `8487.90` | Customs and duty checks on imports | 🔜 |
| `note` | Optional | `5% volume discount per MSA` | Justifies price deviations (already used: a documented rush premium is approved) | ✅ |

## 7. Charges, discounts and tax

| Field | Required | Example | Validation it enables | Status |
|---|---|---|---|---|
| `shipping` / `charges[]` | Optional | freight `150.00` | Included in the total check; freight above [5%] of goods is flagged; freight allowed by Incoterms; `taxable` flag respected | ✅ shipping / 🔜 others |
| `invoice_discounts[]` | Optional | — | Invoice-level discounts in the total check | 🔜 |
| `tax_rate`, `tax_amount` | Should | `0.0825`, `486.75` | Tax = rate × subtotal (`TAX_MISMATCH`) | ✅ |
| `tax_summary[]` | Optional | by jurisdiction | Sum of jurisdictions equals the tax total; rate valid for the ship-to jurisdiction | 🔜 |
| `tax_exempt` + certificate, `reverse_charge`, `withholding_tax` | Optional | `false` | Tax charged on an exempt purchase is an overcharge; reverse-charge VAT handling | 🔜 |

## 8. Totals

| Field | Required | Example | Validation it enables | Status |
|---|---|---|---|---|
| `subtotal` | Should | `5900.00` | Sum of line amounts | ✅ |
| `total` | **Yes** | `6536.75` | Subtotal + tax + charges; positive | ✅ |
| `total_discount`, `total_charges` | Optional | `100.00`, `150.00` | Totals add up | 🔜 |
| `amount_paid`, `credits_applied`, `balance_due` | Optional | `0.00`, `6536.75` | **Pay `balance_due`, not `total`**: avoids paying deposits twice. `total − paid − credits = balance_due`. | 🔜 |
| `amount_in_words` | Optional | "Six thousand five hundred…" | Must spell the same number as `total`. It catches tampered digits, and is standard on cheques. | 🔜 |
| `fx_rate_to_usd` | Optional with FX | `1.08` | Vendor's quoted rate vs. treasury's rate | 🔜 |

## 9. Payment

| Field | Required | Example | Validation it enables | Status |
|---|---|---|---|---|
| `payment_terms` | Should | `2/10 Net 30` | Terms vs. due date (`TERMS_MISMATCH`) | ✅ |
| `payment.early_payment_discount_*` | Optional | 2% within 10 days | **Payment scheduling**: take the discount when it's worth it. 2% on $2M a year is $40K. | 🔜 |
| `payment.late_fee` | Optional | `1.5%/month` | Prioritise invoices that will accrue fees | 🔜 |
| `payment.accepted_methods` | Optional | `ACH`, `check` | Wire-only or crypto-only demands are fraud signals | ✅ via fraud keywords |
| `payment.bank` | Should | bank, holder, last 4, routing | **Must match the bank details on file.** Any difference is `BANK_DETAILS_CHANGED`: escalate and verify by phone. The account holder name must equal the vendor name. | ✅ text signal / 🔜 structured |
| `payment.payment_reference` | Optional | `INV-3001 / ACME-00042` | Reference passed to the bank so the vendor can reconcile | 🔜 |

## 10. Other

| Field | Required | Example | Validation it enables | Status |
|---|---|---|---|---|
| `notes` | Optional | free text | Fraud keywords, prompt-injection detection, LLM risk review | ✅ |
| `terms_and_conditions` | Optional | `Governed by MSA-2025-018` | Contract linkage | 🔜 |
| `authorized_by` | Optional | `Priya Raman, AR Manager` | Signatory matches the vendor's known contacts | 🔜 |
| `attachments` | Optional | packing slip, proof of delivery | Required supporting documents present before payment | 🔜 |

## Where to start

These four would add the most validation value next:

1. **Three-way match.** Add `purchase_orders` and `goods_receipts` tables; check `purchase_order_number` and `po_line_number` against them. This blocks the biggest class of AP loss: paying for things never ordered or never received.
2. **Structured bank verification.** Store each vendor's bank (last 4, routing) in the vendor master and compare it against `payment.bank`, not just keywords. The remit-to address is already compared.
3. **Pay `balance_due` and schedule by terms.** Pay the outstanding amount on the optimal date, taking early-payment discounts.
4. **Vendor tax ID and vendor ID match.** Cheap to add, and very effective against impersonation.
