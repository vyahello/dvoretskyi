"""Bot tools — pure functions over the DB returning plain dicts.

The dispatcher routes deterministically: `TOOLS[name](session, **args)`. Tools never
talk to Telegram or the LLM; they only read/write data and return JSON-able dicts.
Amounts are Decimal internally and stringified at the dict boundary.
"""

from __future__ import annotations

import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from dvoretskyi import clock
from dvoretskyi.agent import meters
from dvoretskyi.agent.submission import channel_for
from dvoretskyi.agent.vision import VisionProvider, get_vision_provider
from dvoretskyi.config import get_settings
from dvoretskyi.db.models import (
    MeterReading,
    MeterStatus,
    NudgeKind,
    Payment,
    PaymentSource,
    Provider,
)
from dvoretskyi.mono import matcher


class ToolError(Exception):
    """Raised for user-correctable problems (unknown provider, bad amount, …)."""


# --- helpers ---------------------------------------------------------------


def _cycle_bounds(cycle: str) -> tuple[datetime, datetime]:
    """[start, end) tz-aware bounds for a 'YYYY-MM' cycle, in Kyiv tz."""
    year, month = (int(p) for p in cycle.split("-"))
    start = datetime(year, month, 1, tzinfo=clock.KYIV)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=clock.KYIV)
    else:
        end = datetime(year, month + 1, 1, tzinfo=clock.KYIV)
    return start, end


def _period_bounds(period: str | None) -> tuple[datetime | None, datetime | None]:
    """Resolve a stats period to [start, end) bounds. None ends = open."""
    if not period or period == "all":
        return None, None
    if len(period) == 4 and period.isdigit():  # "YYYY"
        year = int(period)
        return (
            datetime(year, 1, 1, tzinfo=clock.KYIV),
            datetime(year + 1, 1, 1, tzinfo=clock.KYIV),
        )
    return _cycle_bounds(period)  # "YYYY-MM"


async def _provider_by_name(session: AsyncSession, name: str) -> Provider:
    prov = (
        await session.execute(select(Provider).where(Provider.name == name))
    ).scalar_one_or_none()
    if prov is None:
        # tolerate case / whitespace differences
        wanted = (name or "").strip().casefold()
        for prov in (await session.execute(select(Provider))).scalars():
            if prov.name.casefold() == wanted:
                return prov
        raise ToolError(f"Невідомий провайдер: {name!r}")
    return prov


async def _paid_in_cycle(session: AsyncSession, provider_id: int, cycle: str) -> bool:
    start, end = _cycle_bounds(cycle)
    row = (
        await session.execute(
            select(Payment.id).where(
                Payment.provider_id == provider_id,
                Payment.paid_at >= start,
                Payment.paid_at < end,
            )
        )
    ).first()
    return row is not None


def _parse_amount(value: object) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ToolError(f"Не зрозумів суму: {value!r}") from exc
    if amount <= 0:
        raise ToolError("Сума має бути додатною.")
    return amount


def _parse_until(value: object) -> datetime:
    """Parse a snooze target: ISO datetime/date string, 'N days', or datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=clock.KYIV)
    text = str(value).strip()
    # "3" or "3 days" → relative
    head = text.split()[0] if text else ""
    if head.isdigit():
        return clock.now() + timedelta(days=int(head))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ToolError(f"Не зрозумів дату відкладення: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=clock.KYIV)


# --- tools -----------------------------------------------------------------


async def get_unpaid(session: AsyncSession, cycle: str | None = None) -> dict:
    """Providers with a due_day and no matched payment in the cycle."""
    cycle = cycle or clock.current_cycle()
    is_current = cycle == clock.current_cycle()
    today = clock.now().day

    providers = (
        (
            await session.execute(
                select(Provider)
                .where(Provider.due_day.is_not(None))
                .order_by(Provider.due_day)
            )
        )
        .scalars()
        .all()
    )

    open_items: list[dict] = []
    for prov in providers:
        if await _paid_in_cycle(session, prov.id, cycle):
            continue
        days_left = (prov.due_day - today) if (is_current and prov.due_day) else None
        open_items.append(
            {
                "provider": prov.name,
                "category": prov.category.value,
                "expected_amount": (
                    str(prov.expected_amount)
                    if prov.expected_amount is not None
                    else None
                ),
                "due_day": prov.due_day,
                "days_left": days_left,
            }
        )

    return {"cycle": cycle, "open": open_items, "all_clear": not open_items}


async def get_stats(
    session: AsyncSession, period: str | None = None, breakdown: str = "provider"
) -> dict:
    """Total spend + breakdown by provider or by month, with a PNG chart."""
    start, end = _period_bounds(period)
    conds: list[ColumnElement[bool]] = [Payment.provider_id.is_not(None)]
    if start is not None:
        conds.append(Payment.paid_at >= start)
    if end is not None:
        conds.append(Payment.paid_at < end)

    payments = (await session.execute(select(Payment).where(*conds))).scalars().all()

    total = sum((p.amount_uah for p in payments), Decimal("0"))

    buckets: dict[str, Decimal] = {}
    if breakdown == "month":
        for p in payments:
            key = clock.cycle_of(p.paid_at)
            buckets[key] = buckets.get(key, Decimal("0")) + p.amount_uah
    else:  # provider
        names = {
            prov.id: prov.name
            for prov in (await session.execute(select(Provider))).scalars()
        }
        for p in payments:
            key = names.get(p.provider_id, "?") if p.provider_id is not None else "?"
            buckets[key] = buckets.get(key, Decimal("0")) + p.amount_uah

    items = [
        {
            "label": label,
            "total": str(amount),
            "share": (float(amount / total) if total else 0.0),
        }
        for label, amount in sorted(buckets.items(), key=lambda kv: kv[1], reverse=True)
    ]

    chart_path = (
        _render_chart(buckets, period or clock.current_cycle()) if buckets else None
    )

    return {
        "period": period or clock.current_cycle(),
        "breakdown": breakdown,
        "total": str(total),
        "items": items,
        "chart_path": chart_path,
    }


def _render_chart(buckets: dict[str, Decimal], title: str) -> str:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = list(buckets.keys())
    values = [float(v) for v in buckets.values()]

    fig, ax = plt.subplots(figsize=(7, 4))
    ax.bar(labels, values, color="#3b6ea5")
    ax.set_title(f"Комуналка — {title}")
    ax.set_ylabel("₴")
    fig.autofmt_xdate(rotation=30)
    fig.tight_layout()

    tmp = tempfile.NamedTemporaryFile(
        prefix="dvoretskyi_stats_", suffix=".png", delete=False
    )
    fig.savefig(tmp.name, dpi=120)
    plt.close(fig)
    return tmp.name


async def log_payment_manual(
    session: AsyncSession, provider_name: str, amount: object
) -> dict:
    """Record an off-mono / manual payment (no mono_tx_id)."""
    prov = await _provider_by_name(session, provider_name)
    amount_uah = _parse_amount(amount)
    payment = Payment(
        provider_id=prov.id,
        amount_uah=amount_uah,
        paid_at=clock.now(),
        source=PaymentSource.manual,
        raw_description=f"manual: {prov.name}",
        mcc=None,
        mono_tx_id=None,
    )
    session.add(payment)
    await session.flush()
    return {
        "ok": True,
        "provider": prov.name,
        "amount_uah": str(amount_uah),
        "cycle": clock.current_cycle(),
    }


async def categorize_payment(
    session: AsyncSession, mono_tx_id: str, provider_name: str
) -> dict:
    """Assign an uncategorized webhook payment to a provider and learn its pattern."""
    payment = (
        await session.execute(select(Payment).where(Payment.mono_tx_id == mono_tx_id))
    ).scalar_one_or_none()
    if payment is None:
        raise ToolError(f"Платіж не знайдено: {mono_tx_id}")

    prov = await _provider_by_name(session, provider_name)
    payment.provider_id = prov.id
    learned = await matcher.learn_pattern(session, prov.id, payment.raw_description)
    await session.flush()
    return {
        "ok": True,
        "provider": prov.name,
        "amount_uah": str(payment.amount_uah),
        "learned_pattern": learned.pattern if learned else None,
    }


async def snooze_reminder(
    session: AsyncSession, provider_name: str, until: object
) -> dict:
    """Snooze payment reminders for a provider until a given time."""
    from dvoretskyi.db.models import NudgeLog

    prov = await _provider_by_name(session, provider_name)
    until_dt = _parse_until(until)
    cycle = clock.current_cycle()

    nudge = (
        await session.execute(
            select(NudgeLog).where(
                NudgeLog.provider_id == prov.id,
                NudgeLog.cycle == cycle,
                NudgeLog.kind == NudgeKind.payment,
            )
        )
    ).scalar_one_or_none()
    if nudge is None:
        nudge = NudgeLog(
            provider_id=prov.id,
            cycle=cycle,
            kind=NudgeKind.payment,
            nudged_at=clock.now(),
            snoozed_until=until_dt,
        )
        session.add(nudge)
    else:
        nudge.snoozed_until = until_dt
    await session.flush()
    return {"ok": True, "provider": prov.name, "snoozed_until": until_dt.isoformat()}


# --- meters (L2, Phase 2) --------------------------------------------------


async def _history_values(session: AsyncSession, provider_id: int) -> list[Decimal]:
    """Validated/submitted readings for a provider, most-recent first."""
    rows = (
        (
            await session.execute(
                select(MeterReading)
                .where(
                    MeterReading.provider_id == provider_id,
                    MeterReading.value.is_not(None),
                    MeterReading.status.in_(
                        (MeterStatus.validated, MeterStatus.submitted)
                    ),
                )
                .order_by(MeterReading.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [r.value for r in rows if r.value is not None]


async def _run_submission(
    session: AsyncSession, provider: Provider, reading: MeterReading
) -> dict:
    """Hand a validated reading to its channel; update status/submitted_at."""
    result = await channel_for(provider).submit(provider, reading)
    reading.status = result.status
    if result.submitted:
        reading.submitted_at = clock.now()
    await session.flush()
    msg = result.message
    if result.instructions:
        msg = f"{msg}\n{result.instructions}"
    return {
        "ok": True,
        "reading_id": reading.id,
        "provider": provider.name,
        "value": str(reading.value),
        "status": reading.status.value,
        "consumption": (
            str(reading.consumption_delta)
            if reading.consumption_delta is not None
            else None
        ),
        "message": msg,
        "instructions": result.instructions,
        "deep_link": result.deep_link,
        "submitted": result.submitted,
    }


async def submit_meter_reading(
    session: AsyncSession,
    provider_name: str,
    image_path: str,
    *,
    vision: VisionProvider | None = None,
    reading_id: int | None = None,
) -> dict:
    """Full meter pipeline: OCR → delta-validate → store → submit (channel).

    OCR failure → `value=None`: nothing is submitted; the user is asked to retype.
    A reading that fails delta validation is stored `needs_confirm` and returned for a
    confirm/re-photo prompt — never submitted until confirmed.
    """
    prov = await _provider_by_name(session, provider_name)
    settings = get_settings()
    read = await (vision or get_vision_provider()).read_meter(image_path)

    # Locate/seed the row (an ambiguous-photo capture pre-creates an ocr_pending row).
    reading: MeterReading | None = None
    if reading_id is not None:
        reading = await session.get(MeterReading, reading_id)
    if reading is None:
        reading = MeterReading(
            cycle=clock.current_cycle(),
            status=MeterStatus.ocr_pending,
            created_at=clock.now(),
        )
        session.add(reading)
    reading.provider_id = prov.id
    reading.photo_ref = image_path
    reading.ocr_raw = read.raw or None

    if read.value is None:
        reading.status = MeterStatus.failed
        await session.flush()
        return {
            "ok": False,
            "reading_id": reading.id,
            "provider": prov.name,
            "status": MeterStatus.failed.value,
            "message": (
                "Не зміг розібрати показник на фото. "
                "Перефотографуй ближче або напиши число вручну."
            ),
        }

    # Round to the provider's precision (water=3, gas=2) — source of truth for how many
    # decimals we keep and submit. Delta validation runs on the rounded decimal value.
    quantum = Decimal(1).scaleb(-prov.meter_decimals)
    value = read.value.quantize(quantum, rounding=ROUND_HALF_UP)

    history = await _history_values(session, prov.id)
    verdict = meters.validate(
        value,
        history,
        spike_k=settings.delta_spike_k,
        abs_cap=settings.delta_abs_cap,
    )
    reading.value = value
    reading.consumption_delta = verdict.consumption
    reading.status = verdict.status
    await session.flush()

    if not verdict.ok:
        return {
            "ok": False,
            "reading_id": reading.id,
            "provider": prov.name,
            "value": str(value),
            "status": verdict.status.value,
            "consumption": (
                str(verdict.consumption) if verdict.consumption is not None else None
            ),
            "message": verdict.reason,
        }

    return await _run_submission(session, prov, reading)


async def confirm_meter_reading(session: AsyncSession, reading_id: object) -> dict:
    """User confirmed a `needs_confirm` reading → validate it and run submission."""
    try:
        rid = int(str(reading_id).strip())
    except (TypeError, ValueError) as exc:
        raise ToolError(f"Поганий ідентифікатор показника: {reading_id!r}") from exc

    reading = await session.get(MeterReading, rid)
    if reading is None:
        raise ToolError(f"Показник не знайдено: {rid}")
    if reading.value is None or reading.provider_id is None:
        raise ToolError("Цей показник ще не зчитано — спершу надішли фото.")
    if reading.status is MeterStatus.submitted:
        return {
            "ok": True,
            "reading_id": reading.id,
            "status": reading.status.value,
            "message": "Цей показник уже передано.",
        }

    prov = await session.get(Provider, reading.provider_id)
    if prov is None:
        raise ToolError("Провайдера для цього показника нема.")
    reading.status = MeterStatus.validated
    return await _run_submission(session, prov, reading)


async def mark_meter_submitted(session: AsyncSession, reading_id: object) -> dict:
    """The "відправив" action: user submitted a `validated` reading themselves → mark
    it `submitted`. Button-driven (the bot has the reading_id); not an LLM tool."""
    try:
        rid = int(str(reading_id).strip())
    except (TypeError, ValueError) as exc:
        raise ToolError(f"Поганий ідентифікатор показника: {reading_id!r}") from exc
    reading = await session.get(MeterReading, rid)
    if reading is None:
        raise ToolError(f"Показник не знайдено: {rid}")
    reading.status = MeterStatus.submitted
    reading.submitted_at = clock.now()
    await session.flush()
    return {
        "ok": True,
        "reading_id": reading.id,
        "status": reading.status.value,
        "message": "✅ Зафіксував, що передано.",
    }


async def get_meter_history(
    session: AsyncSession, provider_name: str, limit: int = 6
) -> dict:
    """Recent readings + consumption for a provider (context/stats)."""
    prov = await _provider_by_name(session, provider_name)
    rows = (
        (
            await session.execute(
                select(MeterReading)
                .where(
                    MeterReading.provider_id == prov.id,
                    MeterReading.status.in_(
                        (MeterStatus.validated, MeterStatus.submitted)
                    ),
                )
                .order_by(MeterReading.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {
        "provider": prov.name,
        "readings": [
            {
                "cycle": r.cycle,
                "value": str(r.value) if r.value is not None else None,
                "consumption": (
                    str(r.consumption_delta) if r.consumption_delta is not None else None
                ),
                "status": r.status.value,
            }
            for r in rows
        ],
    }


# --- Phase-2 stub (no balance source yet) ----------------------------------


async def get_provider_balance(session: AsyncSession, provider_name: str) -> dict:
    raise NotImplementedError("Provider-side balance reads need a source (spec §9).")


Tool = Callable[..., Awaitable[dict[str, Any]]]

TOOLS: dict[str, Tool] = {
    "get_unpaid": get_unpaid,
    "get_stats": get_stats,
    "log_payment_manual": log_payment_manual,
    "categorize_payment": categorize_payment,
    "snooze_reminder": snooze_reminder,
    # Meters (L2). submit_meter_reading needs an image_path (supplied by the photo
    # handler, not the LLM); confirm/history are LLM-callable.
    "submit_meter_reading": submit_meter_reading,
    "confirm_meter_reading": confirm_meter_reading,
    "get_meter_history": get_meter_history,
    # Stub until a balance source exists (spec §9):
    "get_provider_balance": get_provider_balance,
}
