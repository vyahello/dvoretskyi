"""Bot tools — pure functions over the DB returning plain dicts.

The dispatcher routes deterministically: `TOOLS[name](session, **args)`. Tools never
talk to Telegram or the LLM; they only read/write data and return JSON-able dicts.
Amounts are Decimal internally and stringified at the dict boundary.
"""

from __future__ import annotations

import logging
import random
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from dvoretskyi import clock, households
from dvoretskyi.agent import meters
from dvoretskyi.agent.submission import channel_for
from dvoretskyi.agent.vision import MeterRead, VisionProvider, get_vision_provider
from dvoretskyi.config import get_settings
from dvoretskyi.db.models import (
    Category,
    Household,
    MeterReading,
    MeterStatus,
    NudgeKind,
    Payment,
    PaymentSource,
    Provider,
)
from dvoretskyi.mono import matcher

log = logging.getLogger(__name__)


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


async def _provider_by_name(
    session: AsyncSession, name: str, household: str | None = None
) -> Provider:
    """Resolve a provider by name. A shared name (ЛЕЗ, Газ доставлення) exists in both
    households → pick the one in `household` if given, else the primary household."""
    wanted = (name or "").strip().casefold()
    matches = [
        p
        for p in (await session.execute(select(Provider))).scalars()
        if p.name.casefold() == wanted
    ]
    if not matches:
        raise ToolError(f"Невідомий провайдер: {name!r}")
    if len(matches) == 1:
        return matches[0]
    want = await households.resolve(session, household) if household else None
    if want is not None:
        for p in matches:
            if p.household_id == want.id:
                return p
    prim = await households.primary(session)
    for p in matches:
        if prim and p.household_id == prim.id:
            return p
    return matches[0]


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


def _stats_caption(label: str, total: Decimal) -> str:
    """The one-line caption that rides with the table image — period + grand total.

    The per-provider breakdown now lives in the rendered table, so we deliberately do
    NOT repeat it here: the caption gives context, the image carries the data.
    """
    return f"📊 {label} — разом {_fmt_uah(total)} ₴"


def _stats_summary(label: str, total: Decimal, items: list[dict], breakdown: str) -> str:
    """Text-only fallback for when no table image is rendered (matplotlib missing /
    chartless test dict). Then the breakdown has nowhere else to go, so it's itemised."""
    lines = [_stats_caption(label, total), ""]
    for it in items:
        amt = Decimal(it["total"])
        share = it.get("share") or 0.0
        # In a by-month view the bucket key is a cycle ("2026-05") → show it in words.
        name = clock.format_cycle(it["label"]) if breakdown == "month" else it["label"]
        lines.append(f"• {name} — {_fmt_uah(amt)} ₴ · {share:.0%}")
    return "\n".join(lines)


async def get_stats(
    session: AsyncSession,
    period: str | None = None,
    breakdown: str = "provider",
    household: str | None = None,
) -> dict:
    """Total spend + breakdown by provider, month, or household, with a PNG chart.

    `household` (slug or address fragment) filters to one property; breakdown="household"
    splits the total across properties. No household + provider/month breakdown =
    combined across both (the default, unchanged)."""
    start, end = _period_bounds(period)

    # provider → (name, household_id); household id → display name (env-seeded).
    provs = (await session.execute(select(Provider))).scalars().all()
    prov_name = {p.id: p.name for p in provs}
    prov_hid = {p.id: p.household_id for p in provs}
    hh_name = {h.id: h.name for h in (await session.execute(select(Household))).scalars()}

    want = await households.resolve(session, household) if household else None

    conds: list[ColumnElement[bool]] = [Payment.provider_id.is_not(None)]
    if start is not None:
        conds.append(Payment.paid_at >= start)
    if end is not None:
        conds.append(Payment.paid_at < end)
    if want is not None:
        ids = [p.id for p in provs if p.household_id == want.id]
        conds.append(Payment.provider_id.in_(ids or [-1]))  # [-1] → matches nothing

    payments = (await session.execute(select(Payment).where(*conds))).scalars().all()

    total = sum((p.amount_uah for p in payments), Decimal("0"))

    buckets: dict[str, Decimal] = {}
    if breakdown == "month":
        for p in payments:
            key = clock.cycle_of(p.paid_at)
            buckets[key] = buckets.get(key, Decimal("0")) + p.amount_uah
    elif breakdown == "household":  # split the total across properties
        for p in payments:
            hid = prov_hid.get(p.provider_id) if p.provider_id is not None else None
            name = hh_name.get(hid) if hid is not None else None
            key = name or households.fallback_label(households.PRIMARY)
            buckets[key] = buckets.get(key, Decimal("0")) + p.amount_uah
    else:  # provider — gas stays split (постачання vs доставлення), each its own line
        for p in payments:
            key = prov_name.get(p.provider_id, "?") if p.provider_id else "?"
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
    if want is not None:  # name the property in the title/caption when filtered
        label = f"{want.name} · {label}"

    # The renderer wants human row labels (by-month buckets are cycles → words).
    rows: list[tuple[str, Decimal, float]] = [
        (
            clock.format_cycle(str(it["label"]))
            if breakdown == "month"
            else str(it["label"]),
            Decimal(str(it["total"])),
            float(it.get("share") or 0.0),  # type: ignore[arg-type]
        )
        for it in items
    ]
    chart_path = _render_table(rows, label, total) if rows else None

    if not items:
        # Empty period (e.g. a month with no payments) must still answer — never hang.
        message = f"За {label} платежів не бачу — порожньо."
    elif chart_path:
        # Table image carries the breakdown → caption is just the period + total.
        message = _stats_caption(label, total)
    else:
        # No image (matplotlib missing) → the text must carry the full breakdown.
        message = _stats_summary(label, total, items, breakdown)

    return {
        "period": period_key,
        "breakdown": breakdown,
        "household": want.slug if want is not None else None,
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


# Modern-table palette (one accent per row; a flat, calm UI look — not 90s bar-chart).
_INK = "#1f2933"  # primary text
_MUTED = "#7b8794"  # secondary text / column headers
_STRIPE = "#f4f6f8"  # zebra row background
_TRACK = "#e4e7eb"  # empty share-bar track


def _render_table(
    rows: list[tuple[str, Decimal, float]], title: str, total: Decimal
) -> str:
    """Render the breakdown as a clean, modern data table (PNG).

    rows: (label, amount, share) sorted biggest-first. Columns: service · mini share-bar
    · amount · share%. Zebra striping, a header band with the grand total, no axes — it
    reads like a card in a finance app, not a matplotlib bar chart.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch, Rectangle

    n = len(rows)
    # Vertical layout in row-units (top → bottom): title band, column header, then rows.
    title_h, header_h, row_h, pad = 1.5, 0.8, 1.0, 0.4
    units = title_h + header_h + n * row_h + pad
    fig, ax = plt.subplots(figsize=(8.6, 0.52 * units + 0.4))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, units)
    ax.axis("off")

    # Column anchors (x in 0..1).
    x_name = 0.045
    x_bar0, x_bar1 = 0.45, 0.70
    x_amount = 0.875  # right-aligned
    x_share = 0.965  # right-aligned

    top = units - pad
    # --- title band: «Комуналка · <період>» left, grand total right ---
    ax.text(x_name, top - 0.55, "Комуналка", fontsize=20, fontweight="bold", color=_INK)
    ax.text(x_name, top - 1.12, title, fontsize=13, color=_MUTED)
    ax.text(
        x_share,
        top - 0.62,
        f"{_fmt_uah(total)} ₴",
        fontsize=20,
        fontweight="bold",
        color=_INK,
        ha="right",
    )
    ax.text(x_share, top - 1.16, "разом", fontsize=12, color=_MUTED, ha="right")

    # --- column header ---
    hy = top - title_h - header_h / 2
    ax.text(x_name, hy, "ПОСЛУГА", fontsize=10.5, color=_MUTED, va="center")
    ax.text(x_amount, hy, "СУМА", fontsize=10.5, color=_MUTED, va="center", ha="right")
    ax.text(x_share, hy, "ЧАСТКА", fontsize=10.5, color=_MUTED, va="center", ha="right")

    # --- data rows ---
    max_share = max((s for _, _, s in rows), default=0.0) or 1.0
    body_top = top - title_h - header_h
    for i, (label, amount, share) in enumerate(rows):
        ry = body_top - i * row_h  # row top edge
        cy = ry - row_h / 2  # row centre
        if i % 2 == 0:
            ax.add_patch(
                Rectangle(
                    (0.02, ry - row_h),
                    0.96,
                    row_h,
                    facecolor=_STRIPE,
                    edgecolor="none",
                    zorder=0,
                )
            )
        accent = _CHART_PALETTE[i % len(_CHART_PALETTE)]
        ax.text(x_name, cy, label, fontsize=13.5, color=_INK, va="center", zorder=2)
        # share mini-bar: full track + accent fill scaled to the biggest share.
        bar_h = row_h * 0.26
        ax.add_patch(
            FancyBboxPatch(
                (x_bar0, cy - bar_h / 2),
                x_bar1 - x_bar0,
                bar_h,
                boxstyle="round,pad=0,rounding_size=0.07",
                facecolor=_TRACK,
                edgecolor="none",
                mutation_aspect=0.4,
                zorder=1,
            )
        )
        fill_w = (x_bar1 - x_bar0) * (share / max_share)
        if fill_w > 0.004:
            ax.add_patch(
                FancyBboxPatch(
                    (x_bar0, cy - bar_h / 2),
                    fill_w,
                    bar_h,
                    boxstyle="round,pad=0,rounding_size=0.07",
                    facecolor=accent,
                    edgecolor="none",
                    mutation_aspect=0.4,
                    zorder=2,
                )
            )
        ax.text(
            x_amount,
            cy,
            f"{_fmt_uah(amount)}",
            fontsize=13.5,
            fontweight="bold",
            color=_INK,
            va="center",
            ha="right",
            zorder=2,
        )
        ax.text(
            x_share,
            cy,
            f"{share:.0%}",
            fontsize=12.5,
            color=_MUTED,
            va="center",
            ha="right",
            zorder=2,
        )

    fig.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.01)
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
    session: AsyncSession,
    mono_tx_id: str,
    provider_name: str,
    household: str | None = None,
) -> dict:
    """Assign an uncategorized webhook payment to a provider and learn its pattern.

    `household` pins the exact property when the provider name is shared (ЛЕЗ, Газ
    доставлення) — the button tap passes it so the tap routes to the chosen home."""
    payment = (
        await session.execute(select(Payment).where(Payment.mono_tx_id == mono_tx_id))
    ).scalar_one_or_none()
    if payment is None:
        raise ToolError(f"Платіж не знайдено: {mono_tx_id}")

    prov = await _provider_by_name(session, provider_name, household)
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
    # Name the meter + value so the confirmation isn't an anonymous «передано».
    provider = (
        await session.get(Provider, reading.provider_id)
        if reading.provider_id is not None
        else None
    )
    meter = provider.name if provider else "показник"
    val = f" — {meter}: {reading.value}" if reading.value is not None else ""
    return {
        "ok": True,
        "reading_id": reading.id,
        "status": reading.status.value,
        "message": f"✅ Зафіксував, що передано{val}.",
    }


_DELETED_LINES = (
    "🗑 Готово — стер {n} показник(ів). Чистий аркуш.",
    "🗑 Прибрав {n} показник(ів) з пам'яті — наче й не було.",
    "🗑 Видалив {n} показник(ів). Порядок.",
    "🗑 Зробив — {n} показник(ів) як корова язиком злизала.",
    "🗑 Готово, {n} показник(ів) більше нема. Аркуш чистий.",
    "🗑 Стер {n} показник(ів) — журнал знову охайний.",
)


def _normalize_cycle(cycle: object) -> str:
    """Coerce a period to a 'YYYY-MM' cycle key ('2026-05-01T…' → '2026-05')."""
    text = str(cycle).strip()
    if len(text) >= 7 and text[4] == "-" and text[:4].isdigit() and text[5:7].isdigit():
        return text[:7]
    raise ToolError(f"Не зрозумів місяць: {cycle!r} (потрібен формат на кшталт 2026-05).")


def _encode_scope(provider_id: int | None, cycle: str | None) -> str:
    """Pack a delete scope into a callback-safe string the confirm button carries.

    'all' (everything) | '<pid>' (one meter, any month) | '<pid|*>:<cycle>' (by month).
    """
    if provider_id is None and cycle is None:
        return "all"
    if cycle is None:
        return str(provider_id)
    return f"{provider_id if provider_id is not None else '*'}:{cycle}"


def _decode_scope(scope: str) -> tuple[int | None, str | None]:
    """Inverse of `_encode_scope` → (provider_id|None, cycle|None)."""
    if scope == "all":
        return None, None
    pid_s, _, cycle = scope.partition(":")
    provider_id = None if pid_s in ("", "*") else int(pid_s)
    return provider_id, (cycle or None)


async def _deletable_readings(
    session: AsyncSession,
    provider_id: int | None = None,
    cycle: str | None = None,
) -> list[MeterReading]:
    """Readings we may drop (anything not already filed on the portal)."""
    conds: list[ColumnElement[bool]] = [MeterReading.status != MeterStatus.submitted]
    if provider_id is not None:
        conds.append(MeterReading.provider_id == provider_id)
    if cycle is not None:
        conds.append(MeterReading.cycle == cycle)
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
    cycle: object | None = None,
) -> dict:
    """Remove stored readings from memory (wrong value entered, etc.).

    Two modes:
    - `reading_id` (the 🗑 button on a specific reading) → an explicit tap, delete now.
    - conversationally (no id) → DON'T delete yet: return a confirmation listing what
      would go, scoped by `provider_name` and/or `cycle` ("YYYY-MM"), so the bot asks
      first. Examples the LLM maps onto args: «видали всі показники» → no scope;
      «видали показник газу» → provider_name; «видали газ за минулий місяць» →
      provider_name + cycle. The bulk deletion happens in `execute_meter_delete` once
      the user confirms.
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
                    f"🗑 Готово, {value} ({name}) більше нема.",
                    f"🗑 Викреслив {value} ({name}) із журналу.",
                )
            ),
        }

    # Conversational → confirm before deleting (never wipe silently).
    provider_id: int | None = None
    if provider_name:
        provider_id = (await _provider_by_name(session, provider_name)).id
    cycle_key = _normalize_cycle(cycle) if cycle else None
    targets = await _deletable_readings(session, provider_id, cycle_key)
    scope_label = _delete_scope_label(provider_name, cycle_key)
    if not targets:
        raise ToolError(f"Не бачу показників{scope_label}, які можна видалити.")
    names = {p.id: p.name for p in (await session.execute(select(Provider))).scalars()}
    preview = "; ".join(
        f"{names.get(r.provider_id or -1, '?')} — {r.value}"
        for r in targets[:6]
        if r.value is not None
    )
    return {
        "ok": True,
        "confirm_delete": True,
        "confirm_scope": _encode_scope(provider_id, cycle_key),
        "count": len(targets),
        "message": (
            f"Знайшов {len(targets)} показник(ів){scope_label}: {preview}. "
            "Точно видалити? Підтвердь кнопкою."
        ),
    }


def _delete_scope_label(provider_name: str | None, cycle: str | None) -> str:
    """' — «Газ (постачання)» за травень 2026' for the confirm/ empty message (or '')."""
    bits = []
    if provider_name:
        bits.append(f"«{provider_name}»")
    if cycle:
        bits.append(f"за {clock.format_cycle(cycle)}")
    return f" {' '.join(bits)}" if bits else ""


async def execute_meter_delete(session: AsyncSession, scope: str) -> dict:
    """Bulk-delete after the user confirms. `scope` is the packed string from
    `_encode_scope` ('all' | '<pid>' | '<pid|*>:<cycle>')."""
    provider_id, cycle = _decode_scope(scope)
    targets = await _deletable_readings(session, provider_id, cycle)
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


async def _portal_reading_for(provider: Provider) -> Any | None:
    """The infolviv portal record for this provider's meter kind, or None if it isn't a
    meter / the portal is unreachable. Kept best-effort: any failure → None (fall back to
    the local journal). Imported lazily to keep the network dep out of the import path."""
    kind = provider.category.value
    if kind not in ("gas", "water"):
        return None
    try:
        from dvoretskyi.agent.infolviv import reading_for_kind

        return await reading_for_kind(kind)
    except Exception:  # network/auth/parse — never let it sink the whole reply
        log.warning("infolviv lookup for %s failed", kind, exc_info=True)
        return None


def _meter_history_message(provider: str, readings: list[dict], portal: Any) -> str:
    """Render readings for the conversational reply (the dispatcher surfaces only
    result["message"]). Portal reachable → its filed record (authoritative) leads, with
    un-filed photo drafts after it; portal unreachable → fall back to the full local
    journal so the question still gets an answer."""
    if portal is not None and portal.value is not None:
        period = clock.format_cycle(portal.period) if portal.period else "останній період"
        cons = f" (спожито {portal.difference})" if portal.difference is not None else ""
        blocks = [
            f"🔢 {provider} — на порталі infolviv:\n• {period}: {portal.value}{cons}"
        ]
        drafts = [
            r for r in readings if r.get("status") in ("validated", "needs_confirm")
        ]
        if drafts:
            lines = "\n".join(
                f"• {clock.format_cycle(r['cycle'])}: {r.get('value') or '—'}"
                for r in drafts
            )
            blocks.append("📝 Збережено з фото (ще не подано на портал):\n" + lines)
        return "\n\n".join(blocks)
    # No portal record → show whatever local history we have.
    if readings:
        lines = "\n".join(
            f"• {clock.format_cycle(r['cycle'])}: {r.get('value') or '—'}"
            + (f" (спожито {r['consumption']})" if r.get("consumption") else "")
            for r in readings
        )
        return f"🔢 {provider} — останні показники:\n" + lines
    return f"Поки що показників по «{provider}» нема — журнал чистий. 🔢"


async def get_meter_history(
    session: AsyncSession,
    provider_name: str,
    limit: int = 6,
    use_portal: bool = True,
) -> dict:
    """Recent readings for a provider. By default consults the **infolviv portal** (the
    authoritative filed value) and adds any un-filed photo drafts — so a conversational
    «покажи показники газу» mirrors the «Мої показники» button. `use_portal=False` keeps
    it local-only (the portal-down fallback journal)."""
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
    readings = [
        {
            "cycle": r.cycle,
            "value": str(r.value) if r.value is not None else None,
            "consumption": (
                str(r.consumption_delta) if r.consumption_delta is not None else None
            ),
            "status": r.status.value,
        }
        for r in rows
    ]
    portal = await _portal_reading_for(prov) if use_portal else None
    return {
        "provider": prov.name,
        "readings": readings,
        "message": _meter_history_message(prov.name, readings, portal),
    }


# --- provider balance (L2) -------------------------------------------------


# Varied phrasings so a repeated «Баланс інтернету» tap never reads like a canned
# autoreply. The numbers (balance/fee/last top-up) stay factual; only the wording rolls.
def _balance_ok_message(
    balance: str, last_topup: str | None, fee: str | None = None
) -> str:
    fee_note = f" (місячна абонплата {fee} ₴)" if fee else ""
    head = random.choice(
        [
            f"Платити не треба — баланс {balance} ₴ перекриває абонплату{fee_note}",
            f"Усе гаразд: {balance} ₴ на рахунку{fee_note}, цього стане надовго",
            f"Інтернет проплачений наперед — {balance} ₴{fee_note}, можна не думати",
            f"Баланс {balance} ₴ — більш ніж досить{fee_note}, не чіпаємо",
            f"З інтернетом тиша: {balance} ₴ у запасі{fee_note}, без приводу хвилюватись",
            f"Хвилюватися нема за що — {balance} ₴ на рахунку{fee_note}, з запасом",
            f"Інтернет у повному порядку: {balance} ₴{fee_note}, абонплата перекрита",
        ]
    )
    if not last_topup:
        return head + "."
    tail = random.choice(
        [
            f". Останнє поповнення — {last_topup}.",
            f" (поповнював {last_topup}).",
            f"; від {last_topup} баланс не чіпав.",
            f". Востаннє докидав {last_topup}.",
        ]
    )
    return head + tail


def _balance_low_message(balance: str, fee: str) -> str:
    return random.choice(
        [
            f"Час поповнити: {balance} ₴ на рахунку — менше за абонплату {fee} ₴.",
            f"Інтернет просить уваги — баланс {balance} ₴ не дотягує до {fee} ₴.",
            f"Треба докинути: {balance} ₴ замало, абонплата {fee} ₴.",
            f"Баланс {balance} ₴ просів нижче за {fee} ₴ — варто поповнити.",
            f"Пора докинути на інтернет: {balance} ₴ проти {fee} ₴ абонплати.",
            f"Інтернет на межі — {balance} ₴, а треба хоча б {fee} ₴.",
        ]
    )


def _mobile_balance_message() -> str:
    return random.choice(
        [
            "Мобільний списується сам (запланований платіж monobank). Захочеш докинути "
            "вручну — ось посилання:",
            "За мобільний не клопочись — autopay monobank усе зробить. Та якщо кортить "
            "поповнити самому:",
            "Мобільний на автопілоті (monobank спише за планом). Ручне поповнення — тут:",
            "Мобільний оплачується автоматично. Як треба поповнити вручну — тримай лінк:",
            "Про мобільний не думай — monobank спише сам. А раптом руки сверблять — ось:",
            "Мобільний веде autopay monobank. Хочеш докинути наперед — лінк нижче:",
        ]
    )


async def get_provider_balance(
    session: AsyncSession, provider_name: str, aspect: str | None = None
) -> dict:
    """Provider balance / top-up link. Gigabit+ → scraped balance + «треба платити?».
    Mobile → just a top-up link (no balance API). Others → not configured.

    `aspect` lets the model answer POINTEDLY instead of dumping everything: "fee" →
    just the monthly subscription, "login" → just the login/contract, "balance"|"pay" →
    balance & whether to top up, None/"all" → the full picture. The owner asked one
    thing; reply to that one thing.
    """
    prov = await _provider_by_name(session, provider_name)
    name = prov.name.casefold()
    aspect = (aspect or "all").casefold()

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
            "message": _mobile_balance_message(),
        }

    if "gigabit" not in name:
        raise NotImplementedError(f"Balance source not configured for {prov.name}.")

    settings = get_settings()
    # The login (contract number) lives in env — the user's own data, returned only to
    # the allowlisted owner. Known even when the cabinet is down, so «який мій логін?» is
    # always answerable without a live fetch.
    login = settings.gigabit_login or None
    login_note = f" Логін (договір): {login}." if login else ""

    # Login-only ask → answer straight from env, no cabinet round-trip needed.
    if aspect == "login":
        msg = f"Логін (договір): {login}." if login else "Логін Gigabit+ не налаштований."
        return {"ok": bool(login), "provider": prov.name, "login": login, "message": msg}

    bal = await fetch_gigabit_balance()
    # Fee falls back to the configured value, so «яка абонплата?» works even if the
    # cabinet is momentarily down.
    fee = bal.monthly_fee or settings.gigabit_monthly_fee

    # Fee-only ask → just the subscription, pointedly.
    if aspect == "fee":
        return {
            "ok": True,
            "provider": prov.name,
            "monthly_fee": str(fee),
            "message": random.choice(
                [
                    f"Місячна абонплата — {fee} ₴.",
                    f"Тариф {fee} ₴ на місяць.",
                    f"Інтернет коштує {fee} ₴ щомісяця.",
                ]
            ),
        }

    if not bal.ok or bal.balance is None:
        # Balance unavailable, but the login is from env — still answer it if asked.
        return {
            "ok": False,
            "provider": prov.name,
            "login": login,
            "message": f"Не зміг дістати баланс Gigabit+ — {bal.note}.{login_note}",
        }

    fee_label = f"{fee:.2f}".rstrip("0").rstrip(".")
    # Balance/pay or full ask. "balance"/"pay" stays focused (no login/fee tail); the
    # default "all" carries the fee in-line and the login note.
    full = aspect not in ("balance", "pay")
    if bal.balance < fee:
        return {
            "ok": True,
            "provider": prov.name,
            "balance": str(bal.balance),
            "monthly_fee": str(fee),
            "login": login,
            "need_to_pay": True,
            "pay_link": gigabit_pay_link(fee),  # rendered as a button, not raw URL
            "pay_label": f"💳 Поповнити {fee_label} ₴",
            "message": _balance_low_message(str(bal.balance), str(fee))
            + (login_note if full else ""),
        }
    return {
        "ok": True,
        "provider": prov.name,
        "balance": str(bal.balance),
        "monthly_fee": str(fee),
        "login": login,
        "need_to_pay": False,
        "message": _balance_ok_message(
            str(bal.balance), bal.last_topup, str(fee) if full else None
        )
        + (login_note if full else ""),
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
