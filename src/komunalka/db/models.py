"""SQLAlchemy 2.0 typed models.

Money is always Decimal (never float). Datetimes are tz-aware, Europe/Kyiv.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Category(str, enum.Enum):
    water = "water"
    electricity = "electricity"
    gas = "gas"
    internet = "internet"
    housing = "housing"
    mobile = "mobile"


class PayChannel(str, enum.Enum):
    mono_communal = "mono_communal"
    mono_card = "mono_card"
    off_mono = "off_mono"


class PatternSource(str, enum.Enum):
    seed = "seed"
    learned = "learned"


class PaymentSource(str, enum.Enum):
    mono_webhook = "mono_webhook"
    manual = "manual"


class NudgeKind(str, enum.Enum):
    payment = "payment"
    meter = "meter"  # scaffolded; inactive in Phase 1


# Numeric(12, 2) — UAH amounts; mapped to Decimal.
_Money = Numeric(12, 2)


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    category: Mapped[Category] = mapped_column(SAEnum(Category, name="category"))
    account_number: Mapped[str | None] = mapped_column(String(64), default=None)
    pay_channel: Mapped[PayChannel] = mapped_column(
        SAEnum(PayChannel, name="pay_channel")
    )
    expected_amount: Mapped[Decimal | None] = mapped_column(_Money, default=None)
    due_day: Mapped[int | None] = mapped_column(Integer, default=None)
    auto_logged: Mapped[bool] = mapped_column(Boolean, default=False)

    patterns: Mapped[list[ProviderPattern]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )
    payments: Mapped[list[Payment]] = relationship(back_populates="provider")


class ProviderPattern(Base):
    __tablename__ = "provider_patterns"
    __table_args__ = (UniqueConstraint("provider_id", "pattern", name="uq_provider_pattern"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"))
    pattern: Mapped[str] = mapped_column(String(255))
    source: Mapped[PatternSource] = mapped_column(SAEnum(PatternSource, name="pattern_source"))

    provider: Mapped[Provider] = relationship(back_populates="patterns")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("providers.id"), default=None
    )  # null = uncategorized
    amount_uah: Mapped[Decimal] = mapped_column(_Money)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[PaymentSource] = mapped_column(SAEnum(PaymentSource, name="payment_source"))
    raw_description: Mapped[str] = mapped_column(String(512), default="")
    mcc: Mapped[int | None] = mapped_column(Integer, default=None)
    mono_tx_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, default=None
    )  # idempotency key; null for manual

    provider: Mapped[Provider | None] = relationship(back_populates="payments")


class NudgeLog(Base):
    __tablename__ = "nudge_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"))
    cycle: Mapped[str] = mapped_column(String(7))  # "YYYY-MM"
    kind: Mapped[NudgeKind] = mapped_column(SAEnum(NudgeKind, name="nudge_kind"))
    nudged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    snoozed_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    provider: Mapped[Provider] = relationship()
