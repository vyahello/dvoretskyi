"""Pydantic models for the monobank webhook StatementItem payload."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from dvoretskyi.clock import KYIV


class StatementItem(BaseModel):
    """A single transaction. `amount` is in kopiykas, negative for outflow."""

    model_config = ConfigDict(extra="ignore")

    id: str
    time: int
    description: str = ""
    # The user's note on the payment (monobank `comment`). For a communal template this
    # often carries the address / особовий рахунок — the thing that distinguishes two
    # properties — so we keep it and match on it, not just `description`.
    comment: str = ""
    counterName: str = ""  # counterparty name, when present
    counterEdrpou: str = ""  # counterparty EDRPOU
    mcc: int | None = None
    amount: int  # kopiykas; negative = outflow
    operationAmount: int | None = None
    currencyCode: int | None = None

    @property
    def is_outflow(self) -> bool:
        return self.amount < 0

    @property
    def match_text(self) -> str:
        """Everything the user/bank attached, joined — so matching & learning can see the
        address/account in `comment`/`counterName`, not only the bare `description`."""
        parts = (self.description, self.comment, self.counterName)
        return " ".join(p for p in parts if p).strip()

    @property
    def amount_uah(self) -> Decimal:
        """Absolute amount in UAH as Decimal (never float)."""
        return (Decimal(abs(self.amount)) / Decimal(100)).quantize(Decimal("0.01"))

    @property
    def paid_at(self) -> datetime:
        return datetime.fromtimestamp(self.time, tz=KYIV)


class StatementData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    account: str | None = None
    statementItem: StatementItem


class WebhookPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    data: StatementData
