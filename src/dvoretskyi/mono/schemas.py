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
    mcc: int | None = None
    amount: int  # kopiykas; negative = outflow
    operationAmount: int | None = None
    currencyCode: int | None = None

    @property
    def is_outflow(self) -> bool:
        return self.amount < 0

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
