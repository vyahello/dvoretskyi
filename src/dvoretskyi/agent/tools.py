"""Bot tools — pure functions over the DB returning plain dicts.

The dispatcher routes deterministically: `TOOLS[name](session, **args)`. Tools never
talk to Telegram or the LLM; they only read/write data and return JSON-able dicts.
Amounts are Decimal internally and stringified at the dict boundary.
"""

from __future__ import annotations

import random
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
from dvoretskyi.agent.vision import MeterRead, VisionProvider, get_vision_provider
from dvoretskyi.config import get_settings
from dvoretskyi.db.models import (
    Category,
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


# Meteorological seasons → Ukrainian label. Winter wraps the year boundary (Dec..Feb).
_SEASON_LABEL = {
    "winter": "зима",
    "зима": "зима",
    "spring": "весна",
    "весна": "весна",
    "summer": "літо",
    "літо": "літо",
    "autumn": "осінь",
    "fall": "осінь",
    "осінь": "осінь",
}
_SEASON_START_MONTH = {"зима": 12, "весна": 3, "літо": 6, "осінь": 9}


def _season_parts(period: str) -> tuple[str, int] | None:
    """('зима', 2026) from 'winter 2026' / 'зима-2026' / 'літо' (→ current year)."""
    season: str | None = None
    year: int | None = None
    for tok in period.strip().lower().replace(":", " ").replace("-", " ").split():
        if tok in _SEASON_LABEL:
            season = _SEASON_LABEL[tok]
        elif len(tok) == 4 and tok.isdigit():
            year = int(tok)
    if season is None:
        return None
    return season, year or clock.now().year


def _season_bounds(season: str, year: int) -> tuple[datetime, datetime]:
    sm = _SEASON_START_MONTH[season]
    if season == "зима":  # Dec (year-1) → Mar (year)
        return (
            datetime(year - 1, 12, 1, tzinfo=clock.KYIV),
            datetime(year, 3, 1, tzinfo=clock.KYIV),
        )
    em = sm + 3
    return (
        datetime(year, sm, 1, tzinfo=clock.KYIV),
        datetime(year, em, 1, tzinfo=clock.KYIV),
    )


def _period_bounds(period: str | None) -> tuple[datetime | None, datetime | None]:
    """Resolve a stats period to [start, end) bounds. None ends = open.

    Accepts 'all', 'YYYY', 'YYYY-MM', and a season (зима/літо/весна/осінь, optional year)
    so the agent can answer «скільки за зиму» as a real 3-month range."""
    if not period or period == "all":
        return None, None
    parts = _season_parts(period)
    if parts is not None:
        return _season_bounds(*parts)
    if len(period) == 4 and period.isdigit():  # "YYYY"
        year = int(period)
        return (
            datetime(year, 1, 1, tzinfo=clock.KYIV),
            datetime(year + 1, 1, 1, tzinfo=clock.KYIV),
        )
    return _cycle_bounds(period)  # "YYYY-MM"


def _period_label(period: str | None) -> str:
    """Ukrainian label: 'весь час' / '2026 рік' / 'зима 2026' / 'травень 2026'."""
    if not period or period == "all":
        return "весь час"
    parts = _season_parts(period)
    if parts is not None:
        season, year = parts
        return f"{season} {year}"
    if len(period) == 4 and period.isdigit():
        return f"{period} рік"
    return clock.format_cycle(period)


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

    # Mobile is auto-paid (scheduled mono payment) → not nagged (due_day=None, so it's
    # not in `open`), but surfaced separately so we don't claim "все оплачено" before it
    # actually charges this cycle.
    auto_pending: list[dict] = []
    mobile_provs = (
        (
            await session.execute(
                select(Provider).where(Provider.category == Category.mobile)
            )
        )
        .scalars()
        .all()
    )
    autopay_day = get_settings().mobile_autopay_day
    for prov in mobile_provs:
        if not await _paid_in_cycle(session, prov.id, cycle):
            auto_pending.append(
                {
                    "provider": prov.name,
                    "category": prov.category.value,
                    "autopay_day": autopay_day,
                }
            )

    return {
        "cycle": cycle,
        "open": open_items,
        "all_clear": not open_items,
        "auto_pending": auto_pending,
    }


def _fmt_uah(amount: Decimal) -> str:
    """'2391.39' → '2 391.39' (space-grouped thousands, always 2 decimals)."""
    cents = int(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole:,}".replace(",", " ") + f".{frac:02d}"


def _stats_summary(label: str, total: Decimal, items: list[dict], breakdown: str) -> str:
    """A readable, itemised summary — header + one bullet per group with its share.

    Beats the old one-liner: «найбільше з'їв X» told you the top line and hid the rest.
    """
    head = f"📊 {label} — разом {_fmt_uah(total)} ₴"
    lines = [head, ""]
    for it in items:
        amt = Decimal(it["total"])
        share = it.get("share") or 0.0
        # In a by-month view the bucket key is a cycle ("2026-05") → show it in words.
        name = clock.format_cycle(it["label"]) if breakdown == "month" else it["label"]
        lines.append(f"• {name} — {_fmt_uah(amt)} ₴ · {share:.0%}")
    return "\n".join(lines)


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
    else:  # provider — gas stays split (постачання vs доставлення), each its own line
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

    period_key = period or clock.current_cycle()
    label = _period_label(period_key)

    chart_path = _render_chart(buckets, label) if buckets else None

    if not items:
        # Empty period (e.g. a month with no payments) must still answer — never hang.
        message = f"За {label} платежів не бачу — порожньо."
    else:
        message = _stats_summary(label, total, items, breakdown)

    return {
        "period": period_key,
        "breakdown": breakdown,
        "total": str(total),
        "items": items,
        "chart_path": chart_path,
        "message": message,
    }


# Distinct colour per group, cycled if there are more groups than colours.
_CHART_PALETTE = (
    "#2a9d8f",
    "#e76f51",
    "#e9c46a",
    "#264653",
    "#8ab17d",
    "#f4a261",
    "#5b8e7d",
    "#bc4749",
    "#457b9d",
    "#9d4edd",
)


def _render_chart(buckets: dict[str, Decimal], title: str) -> str:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Biggest on top → reads like a ranking. Horizontal bars fit Ukrainian labels.
    items = sorted(buckets.items(), key=lambda kv: kv[1])
    labels = [k for k, _ in items]
    values = [float(v) for _, v in items]
    colors = [_CHART_PALETTE[i % len(_CHART_PALETTE)] for i in range(len(labels))]
    total = sum(values)

    fig, ax = plt.subplots(figsize=(8.5, 0.85 * len(labels) + 1.8))
    bars = ax.barh(labels, values, color=colors, edgecolor="white", height=0.66)
    ax.set_title(
        f"Комуналка — {title}  ·  {total:,.0f} ₴".replace(",", " "),
        fontsize=16,
        fontweight="bold",
        pad=14,
    )

    # Big, bold value + share label. Long bars: label INSIDE (white, right-aligned) so
    # it never clips the frame; short bars: OUTSIDE (dark, left-aligned).
    xmax = max(values) if values else 1.0
    for bar, val in zip(bars, values, strict=False):
        share = f"   {val / total:.0%}" if total else ""
        text = f"{val:,.0f} ₴{share}".replace(",", " ")
        inside = bar.get_width() > xmax * 0.55
        ax.text(
            bar.get_width() - (xmax * 0.012 if inside else -xmax * 0.012),
            bar.get_y() + bar.get_height() / 2,
            text,
            va="center",
            ha="right" if inside else "left",
            fontsize=14,
            fontweight="bold",
            color="white" if inside else "#264653",
        )

    ax.set_xlim(0, xmax * 1.30)  # headroom for the outside labels
    for side in ("top", "right", "bottom"):
        ax.spines[side].set_visible(False)
    # Big group labels on the left; bars are self-labelled so no x-axis.
    ax.tick_params(left=False, bottom=False, labelbottom=False, labelsize=14)
    for lbl in ax.get_yticklabels():
        lbl.set_fontweight("bold")
    fig.tight_layout()

    tmp = tempfile.NamedTemporaryFile(
        prefix="dvoretskyi_stats_", suffix=".png", delete=False
    )
    fig.savefig(tmp.name, dpi=150)
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
    """Snooze reminders for a provider until a given time.

    Snoozes the payment nudge, and — for a balance-tracked provider (Gigabit+) — the
    low-balance nudge too, so "відклади інтернет" silences both."""
    from dvoretskyi.db.models import NudgeLog

    prov = await _provider_by_name(session, provider_name)
    until_dt = _parse_until(until)
    cycle = clock.current_cycle()

    kinds = [NudgeKind.payment]
    if "gigabit" in prov.name.casefold():
        kinds.append(NudgeKind.balance)

    for kind in kinds:
        nudge = (
            await session.execute(
                select(NudgeLog).where(
                    NudgeLog.provider_id == prov.id,
                    NudgeLog.cycle == cycle,
                    NudgeLog.kind == kind,
                )
            )
        ).scalar_one_or_none()
        if nudge is None:
            session.add(
                NudgeLog(
                    provider_id=prov.id,
                    cycle=cycle,
                    kind=kind,
                    nudged_at=clock.now(),
                    snoozed_until=until_dt,
                )
            )
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


async def _supersede_pending(
    session: AsyncSession, provider_id: int, keep_id: int | None
) -> None:
    """Drop this meter's older un-filed readings — we keep & submit only the freshest.

    A fresh photo of a meter replaces any earlier draft of the SAME meter that hasn't
    been filed yet, so the journal never accumulates stale duplicates (that confused the
    user: «виглядає як 3 фото»). Submitted readings are the permanent record — untouched.
    """
    rows = (
        (
            await session.execute(
                select(MeterReading).where(
                    MeterReading.provider_id == provider_id,
                    MeterReading.id != keep_id,
                    MeterReading.status != MeterStatus.submitted,
                )
            )
        )
        .scalars()
        .all()
    )
    for r in rows:
        await session.delete(r)
    if rows:
        await session.flush()


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


def _validated_result(prov: Provider, reading: MeterReading) -> dict:
    """A stored, validated reading that is NOT yet submitted (awaiting the date gate)."""
    return {
        "ok": True,
        "reading_id": reading.id,
        "provider": prov.name,
        "kind": prov.category.value,
        "value": str(reading.value),
        "status": MeterStatus.validated.value,
        "consumption": (
            str(reading.consumption_delta)
            if reading.consumption_delta is not None
            else None
        ),
        "message": "Записав показник.",
    }


async def submit_meter_reading(
    session: AsyncSession,
    provider_name: str,
    image_path: str,
    *,
    vision: VisionProvider | None = None,
    reading_id: int | None = None,
    read: MeterRead | None = None,
    auto_submit: bool = True,
) -> dict:
    """Full meter pipeline: OCR → delta-validate → store → submit (channel).

    OCR failure → `value=None`: nothing is submitted; the user is asked to retype.
    A reading that fails delta validation is stored `needs_confirm` and returned for a
    confirm/re-photo prompt — never submitted until confirmed. `read` lets the caller
    pass an already-OCR'd MeterRead (e.g. the photo handler) to avoid a second call.
    `auto_submit=False` stores the validated reading but defers submission to the caller
    (the bot's date-gated approve/insistence flow) instead of running the channel now.
    """
    prov = await _provider_by_name(session, provider_name)
    settings = get_settings()
    if read is None:
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
            "kind": prov.category.value,
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
    # A fresh reading for this meter supersedes any earlier un-filed draft of it.
    await _supersede_pending(session, prov.id, reading.id)

    if not verdict.ok:
        return {
            "ok": False,
            "reading_id": reading.id,
            "provider": prov.name,
            "kind": prov.category.value,
            "value": str(value),
            "status": verdict.status.value,
            "consumption": (
                str(verdict.consumption) if verdict.consumption is not None else None
            ),
            "message": verdict.reason,
        }

    if not auto_submit:
        return _validated_result(prov, reading)
    return await _run_submission(session, prov, reading)


async def confirm_meter_reading(
    session: AsyncSession, reading_id: object, *, auto_submit: bool = True
) -> dict:
    """User confirmed a `needs_confirm` reading → validate it (and maybe run submission).

    `auto_submit=False` validates without submitting — the bot then runs its date-gated
    approve/insistence flow, mirroring the photo path."""
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
    if not auto_submit:
        await session.flush()
        return _validated_result(prov, reading)
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


_DELETED_LINES = (
    "🗑 Готово — стер {n} показник(ів). Чистий аркуш.",
    "🗑 Прибрав {n} показник(ів) з пам'яті — наче й не було.",
    "🗑 Видалив {n} показник(ів). Порядок.",
)


async def _deletable_readings(
    session: AsyncSession, provider_id: int | None = None
) -> list[MeterReading]:
    """Readings we may drop (anything not already filed on the portal)."""
    conds: list[ColumnElement[bool]] = [MeterReading.status != MeterStatus.submitted]
    if provider_id is not None:
        conds.append(MeterReading.provider_id == provider_id)
    rows = (
        await session.execute(
            select(MeterReading).where(*conds).order_by(MeterReading.created_at.desc())
        )
    ).scalars()
    return list(rows)


async def delete_meter_reading(
    session: AsyncSession,
    provider_name: str | None = None,
    *,
    reading_id: object | None = None,
) -> dict:
    """Remove stored readings from memory (wrong value entered, etc.).

    Two modes:
    - `reading_id` (the 🗑 button on a specific reading) → an explicit tap, delete now.
    - conversationally (no id) → DON'T delete yet: return a confirmation listing what
      would go (all readings, or just `provider_name`'s), so the bot asks first. The bulk
      deletion happens in `execute_meter_delete` once the user confirms.
    A reading already filed on the portal (`submitted`) can't be un-filed there — we keep
    it and say so. Deletes are hard (the row is gone, so it stops skewing history)."""
    if reading_id is not None:
        try:
            reading = await session.get(MeterReading, int(str(reading_id).strip()))
        except (TypeError, ValueError) as exc:
            raise ToolError(f"Поганий ідентифікатор показника: {reading_id!r}") from exc
        if reading is None:
            raise ToolError("Такого показника не знайшов — нема що видаляти.")
        if reading.status is MeterStatus.submitted:
            raise ToolError(
                "Цей показник уже подано на портал — звідти його прибрати я не можу, "
                "лише на самому infolviv."
            )
        owner = (
            await session.get(Provider, reading.provider_id)
            if reading.provider_id is not None
            else None
        )
        value = str(reading.value) if reading.value is not None else "—"
        name = owner.name if owner else "лічильник"
        await session.delete(reading)
        await session.flush()
        return {
            "ok": True,
            "provider": name,
            "value": value,
            "message": random.choice(
                (
                    f"🗑 Прибрав показник {value} ({name}).",
                    f"🗑 Стер {value} ({name}) з пам'яті.",
                    f"🗑 Видалив {value} ({name}) — як не було.",
                )
            ),
        }

    # Conversational → confirm before deleting (never wipe silently).
    provider_id: int | None = None
    if provider_name:
        provider_id = (await _provider_by_name(session, provider_name)).id
    targets = await _deletable_readings(session, provider_id)
    if not targets:
        raise ToolError("Не бачу показників, які можна видалити.")
    names = {p.id: p.name for p in (await session.execute(select(Provider))).scalars()}
    preview = "; ".join(
        f"{names.get(r.provider_id or -1, '?')} — {r.value}"
        for r in targets[:6]
        if r.value is not None
    )
    return {
        "ok": True,
        "confirm_delete": True,
        "confirm_scope": "all" if provider_id is None else str(provider_id),
        "count": len(targets),
        "message": (
            f"У пам'яті {len(targets)} показник(ів): {preview}. "
            "Точно видалити? Підтвердь кнопкою."
        ),
    }


async def execute_meter_delete(session: AsyncSession, scope: str) -> dict:
    """Bulk-delete after the user confirms. `scope` = 'all' or a provider id (str)."""
    provider_id = None if scope == "all" else int(scope)
    targets = await _deletable_readings(session, provider_id)
    for reading in targets:
        await session.delete(reading)
    await session.flush()
    if not targets:
        return {"ok": True, "deleted": 0, "message": "Уже порожньо — нема чого стирати."}
    return {
        "ok": True,
        "deleted": len(targets),
        "message": random.choice(_DELETED_LINES).format(n=len(targets)),
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


# --- provider balance (L2) -------------------------------------------------


async def get_provider_balance(session: AsyncSession, provider_name: str) -> dict:
    """Provider balance / top-up link. Gigabit+ → scraped balance + «треба платити?».
    Mobile → just a top-up link (no balance API). Others → not configured."""
    prov = await _provider_by_name(session, provider_name)
    name = prov.name.casefold()

    from dvoretskyi.agent.balance import (
        fetch_gigabit_balance,
        gigabit_pay_link,
        mobile_pay_link,
    )

    if "мобільн" in name:
        return {
            "ok": True,
            "provider": prov.name,
            "pay_link": mobile_pay_link(),
            "pay_label": "💳 Поповнити мобільний",
            "message": (
                "Мобільний оплачується автоматично (запланований платіж у monobank). "
                "Якщо хочеш поповнити вручну — ось посилання:"
            ),
        }

    if "gigabit" not in name:
        raise NotImplementedError(f"Balance source not configured for {prov.name}.")

    settings = get_settings()
    bal = await fetch_gigabit_balance()
    if not bal.ok or bal.balance is None:
        return {
            "ok": False,
            "provider": prov.name,
            "message": f"Не зміг дізнатися баланс Gigabit+ — {bal.note}.",
        }

    # Fee straight from the cabinet tariff; config value is only a fallback.
    fee = bal.monthly_fee or settings.gigabit_monthly_fee
    fee_label = f"{fee:.2f}".rstrip("0").rstrip(".")
    if bal.balance < fee:
        return {
            "ok": True,
            "provider": prov.name,
            "balance": str(bal.balance),
            "monthly_fee": str(fee),
            "need_to_pay": True,
            "pay_link": gigabit_pay_link(fee),  # rendered as a button, not raw URL
            "pay_label": f"💳 Поповнити {fee_label} ₴",
            "message": (
                f"Треба поповнити: баланс {bal.balance} ₴ — менший за абонплату {fee} ₴."
            ),
        }
    tail = f", останнє поповнення {bal.last_topup}" if bal.last_topup else ""
    return {
        "ok": True,
        "provider": prov.name,
        "balance": str(bal.balance),
        "monthly_fee": str(fee),
        "need_to_pay": False,
        "message": f"Платити не треба — баланс {bal.balance} ₴ достатній{tail}.",
    }


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
    "delete_meter_reading": delete_meter_reading,
    "get_meter_history": get_meter_history,
    # Stub until a balance source exists (spec §9):
    "get_provider_balance": get_provider_balance,
}
