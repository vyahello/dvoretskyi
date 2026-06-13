"""Bot tools — pure functions over the DB returning plain dicts.

The dispatcher routes deterministically: `TOOLS[name](session, **args)`. Tools never
talk to Telegram or the LLM; they only read/write data and return JSON-able dicts.
Amounts are Decimal internally and stringified at the dict boundary.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from komunalka import clock
from komunalka.db.models import (
    NudgeKind,
    Payment,
    PaymentSource,
    Provider,
)
from komunalka.mono import matcher


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
    if period in (None, "", "all"):
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
        await session.execute(
            select(Provider).where(Provider.due_day.is_not(None)).order_by(Provider.due_day)
        )
    ).scalars().all()

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
                    str(prov.expected_amount) if prov.expected_amount is not None else None
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
    conds = [Payment.provider_id.is_not(None)]
    if start is not None:
        conds.append(Payment.paid_at >= start)
    if end is not None:
        conds.append(Payment.paid_at < end)

    payments = (
        await session.execute(
            select(Payment).where(*conds)
        )
    ).scalars().all()

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
            key = names.get(p.provider_id, "?")
            buckets[key] = buckets.get(key, Decimal("0")) + p.amount_uah

    items = [
        {
            "label": label,
            "total": str(amount),
            "share": (float(amount / total) if total else 0.0),
        }
        for label, amount in sorted(buckets.items(), key=lambda kv: kv[1], reverse=True)
    ]

    chart_path = _render_chart(buckets, period or clock.current_cycle()) if buckets else None

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

    tmp = tempfile.NamedTemporaryFile(prefix="komunalka_stats_", suffix=".png", delete=False)
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
        await session.execute(
            select(Payment).where(Payment.mono_tx_id == mono_tx_id)
        )
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
    from komunalka.db.models import NudgeLog

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


# --- Phase-2 stubs (signatures only) ---------------------------------------

async def submit_meter_reading(
    session: AsyncSession, provider_name: str, value: object, photo: object = None
) -> dict:
    raise NotImplementedError("Meter submission is Phase 2.")


async def get_provider_balance(session: AsyncSession, provider_name: str) -> dict:
    raise NotImplementedError("Provider-side balance reads are Phase 2.")


TOOLS = {
    "get_unpaid": get_unpaid,
    "get_stats": get_stats,
    "log_payment_manual": log_payment_manual,
    "categorize_payment": categorize_payment,
    "snooze_reminder": snooze_reminder,
    # Phase-2 (present so the LLM knows they exist; raise if called):
    "submit_meter_reading": submit_meter_reading,
    "get_provider_balance": get_provider_balance,
}
