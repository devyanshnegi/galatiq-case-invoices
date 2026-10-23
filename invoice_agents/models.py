"""Typed contracts passed between agents. These double as the LLMs' structured-output schemas."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class LineItem(BaseModel):
    item: str = Field(description="Product name exactly as written on the invoice, e.g. 'Widget A' or 'WidgetA (rush order)'")
    quantity: float | None = Field(default=None, description="Quantity ordered. Keep negative numbers negative. null if missing.")
    unit_price: float | None = Field(default=None, description="Price per unit as a plain number. null if missing.")
    amount: float | None = Field(default=None, description="Line total as written on the invoice. null if not shown.")
    discount: float | None = Field(default=None, description="Discount on this line as a money amount (not a percent). null if none.")
    note: str | None = Field(default=None, description="Any note on this line, e.g. 'rush order', 'volume discount'")
    sku: str | None = Field(default=None, description="Leave null. Filled in later by catalog matching.")


class Invoice(BaseModel):
    """Canonical invoice. Every source format is normalised into this."""

    invoice_number: str | None = Field(default=None, description="Invoice number, e.g. 'INV-1002'")
    vendor: str | None = Field(default=None, description="Name of the company that issued the invoice (the seller, not Acme Corp)")
    vendor_address: str | None = None
    remit_to_address: str | None = Field(default=None, description="'Remit to' / pay-to address if different from the vendor address")
    invoice_date: str | None = Field(default=None, description="Invoice date as YYYY-MM-DD. null if missing or unreadable.")
    due_date: str | None = Field(default=None, description="Due date as YYYY-MM-DD. null if missing or not an actual date.")
    due_date_raw: str | None = Field(default=None, description="Due date text exactly as written, e.g. 'yesterday'")
    currency: str = Field(default="USD", description="ISO currency code; USD if a $ sign is used or none is stated")
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float | None = None
    tax: float | None = Field(default=None, description="Tax amount (not rate)")
    tax_rate: float | None = Field(default=None, description="Tax rate as a fraction, e.g. 0.05 for 5%")
    shipping: float | None = None
    total: float | None = Field(default=None, description="Final amount due")
    payment_terms: str | None = None
    notes: str | None = Field(default=None, description="Free-text notes, remarks or instructions on the invoice")
    revision: str | None = Field(default=None, description="Revision marker if this is a revised invoice")


class Severity(str, Enum):
    ERROR = "error"  # blocks payment
    WARNING = "warning"  # needs human/VP judgement
    INFO = "info"  # worth knowing, no action


class Issue(BaseModel):
    code: str
    severity: Severity
    message: str
    field: str | None = None


class ValidationReport(BaseModel):
    issues: list[Issue] = Field(default_factory=list)
    checks_run: list[str] = Field(default_factory=list)
    total_usd: float | None = None
    risk_score: int = 0  # 0-100, higher is riskier

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]

    @property
    def passed(self) -> bool:
        return not self.errors


class RiskSignal(BaseModel):
    category: Literal["social_engineering", "implausible_vendor_details", "payment_redirection", "document_tampering", "other"]
    evidence: str = Field(description="The exact text from the document that is suspicious, and why, in one sentence")
    severity: Literal["high", "low"] = Field(description="high = a careful AP clerk would refuse to pay without checking; low = merely unusual")


class RiskAssessment(BaseModel):
    """LLM fraud-analyst output: soft signals that rules can't express."""

    suspicious: bool = Field(description="True only if there is at least one high-severity signal")
    signals: list[RiskSignal] = Field(default_factory=list)


Decision = Literal["approve", "reject", "escalate"]


class ApprovalDecision(BaseModel):
    decision: Decision = Field(description="approve = pay now; reject = do not pay; escalate = needs a human")
    reasoning: str = Field(description="2-4 sentences a VP of Finance could read, citing the specific facts")
    key_factors: list[str] = Field(default_factory=list, description="The facts that drove the decision")
    conditions: list[str] = Field(default_factory=list, description="Follow-ups required, if any")


class Critique(BaseModel):
    agrees: bool = Field(description="True if the decision is correct and well-justified under the policy")
    problems: list[str] = Field(default_factory=list, description="Specific errors in the decision or reasoning")
    recommended_decision: Decision = Field(description="What the decision should be")


class ExtractionCheck(BaseModel):
    ok: bool
    problems: list[str] = Field(default_factory=list)
