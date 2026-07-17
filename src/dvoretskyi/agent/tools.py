"""Bot tools — pure functions over the DB returning plain dicts.

The dispatcher routes deterministically: `TOOLS[name](session, **args)`. Tools never
talk to Telegram or the LLM; they only read/write data and return JSON-able dicts.
Amounts are Decimal internally and stringified at the dict boundary.
"""

from __future__ import annotations

import asyncio
import html
import logging
import random
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from dvoretskyi import clock, households
from dvoretskyi.agent import charts, meters, photo_store
from dvoretskyi.agent.charts import fmt_uah as _fmt_uah
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
    """[start, end) tz-aware bounds for a 'YYYY-MM' cycle, in Kyiv tz.

    Raises ToolError (never a bare ValueError) on anything that isn't a real month —
    the dispatcher surfaces ToolError to the user, while a ValueError would kill the
    whole turn.
    """
    try:
        year, month = (int(p) for p in str(cycle).split("-"))
        if not 1 <= month <= 12:
            raise ValueError(f"month out of range: {month}")
        start = datetime(year, month, 1, tzinfo=clock.KYIV)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ToolError(f"Не зрозумів період: {cycle!r}") from exc
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


# A rolling window the user asks for in words: «за пів року», «останні 6 місяців», «6m».
# Without this the model had no way to express a relative range, so it invented free text
# that crashed the turn on int() — «динаміка за пів року» being the owner's own example.
_REL_MONTHS_RE = re.compile(r"(?:^|\D)(\d{1,2})\s*(?:m\b|міс)", re.IGNORECASE)
_HALF_YEAR_RE = re.compile(r"пів\s*року|півроку|half.?year", re.IGNORECASE)


def _relative_months(period: str) -> int | None:
    """Months in a rolling window, or None if `period` isn't one."""
    if _HALF_YEAR_RE.search(period):
        return 6
    match = _REL_MONTHS_RE.search(period)
    if match:
        return max(1, min(24, int(match.group(1))))
    return None


def _rolling_bounds(months: int) -> tuple[datetime, datetime]:
    """[start of the month `months-1` back, start of next month) — a window that ends
    with the CURRENT month included, which is what «останні N місяців» means."""
    now = clock.now()
    start, _ = _cycle_bounds(clock.shift_cycle(clock.cycle_of(now), -(months - 1)))
    _, end = _cycle_bounds(clock.cycle_of(now))
    return start, end


def _period_bounds(period: str | None) -> tuple[datetime | None, datetime | None]:
    """Resolve a stats period to [start, end) bounds. None ends = open.

    Accepts 'all', 'YYYY', 'YYYY-MM', a season (зима/літо/весна/осінь, optional year) so
    «скільки за зиму» is a real 3-month range, and a rolling window («пів року»,
    «останні 6 місяців», '6m'). Anything else raises ToolError rather than ValueError,
    so a period the model invents costs the user a sentence, not the whole turn.
    """
    if period is None:
        return None, None
    period = str(period).strip()
    if not period or period.casefold() in ("all", "весь час", "завжди"):
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
    rel = _relative_months(period)
    if rel is not None:
        return _rolling_bounds(rel)
    return _cycle_bounds(period)  # "YYYY-MM" (raises ToolError if it isn't)


def _period_label(period: str | None) -> str:
    """Ukrainian label: 'весь час' / '2026 рік' / 'зима 2026' / 'травень 2026' /
    'останні 6 міс.'."""
    if period is None:
        return "весь час"
    period = str(period).strip()
    if not period or period.casefold() in ("all", "весь час", "завжди"):
        return "весь час"
    parts = _season_parts(period)
    if parts is not None:
        season, year = parts
        return f"{season} {year}"
    if len(period) == 4 and period.isdigit():
        return f"{period} рік"
    rel = _relative_months(period)
    if rel is not None:
        return f"останні {rel} міс."
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


# Display order for provider sections/rows across the journal, payment history and plan:
# gas, water, electricity, housing (кварплата), internet, mobile — the order the user
# reads bills in. Unknown categories sort last; within a category, home household first
# (the primary household is seeded first, so the lower id sorts ahead).
_CATEGORY_ORDER = {
    "gas": 0,
    "water": 1,
    "electricity": 2,
    "housing": 3,
    "internet": 4,
    "mobile": 5,
}


def _provider_order_key(prov: Provider) -> tuple[int, int]:
    return (_CATEGORY_ORDER.get(prov.category.value, 99), prov.household_id or 0)


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


# The widest by-month chart that still reads on a phone. Past this the bars are hairlines
# and the month labels collide, so an over-long window is clamped to the recent end.
_MAX_MONTH_COLUMNS = 24


def _month_ticks(cycles: list[str]) -> list[str]:
    """Axis labels for a month series: «чер», or «чер 26» once the window crosses a year.

    A bare month name repeats every 12 months, so a 24-month window drew «чер» twice and
    the reader had no way to tell which year a column belonged to.
    """
    multi_year = len({c.split("-")[0] for c in cycles}) > 1
    return [clock.format_cycle_short(c, with_year=multi_year) for c in cycles]


def _cycles_between(
    buckets: dict[str, Decimal], start: datetime | None, end: datetime | None
) -> list[str]:
    """Every cycle key the by-month view should show.

    Bounded by the requested period where it has bounds; for an open-ended period
    ('all') we span the data itself, so an empty month between two paid ones still
    appears as a zero without inventing a runway of months before the journal begins.

    The window never runs past the CURRENT month. «Цей рік» has bounds through December,
    which zero-filled five months of future into the chart — months that cannot have a
    payment yet, drawn as if they were months you spent nothing. (A future-dated payment,
    should one ever exist, still extends the window: `max(buckets)` wins.)
    """
    if not buckets:
        return []
    first = clock.cycle_of(start) if start is not None else min(buckets)
    # `end` is exclusive (the 1st of the next month) → step back a day for its cycle.
    last = clock.cycle_of(end - timedelta(days=1)) if end is not None else max(buckets)
    last = min(last, clock.current_cycle())
    first, last = min(first, min(buckets)), max(last, max(buckets))
    span = clock.months_between(first, last)
    return [clock.shift_cycle(first, i) for i in range(span + 1)]


# Category → its first colour slot in `charts._SLOTS`. Gas owns TWO (постачання +
# доставлення are separate bills the user reads separately), everything else owns one.
# This is a FIXED map, not a running counter: the counter made a service's colour depend
# on which other providers happened to exist, so water was blue in production and aqua in
# a 4-provider test. Anchoring to the category means water is blue, always. The resulting
# order is the one validated for colour-vision separation — see `charts._SLOTS`.
_CATEGORY_SLOT = {
    "gas": 0,  # + 1 for the second gas name
    "water": 2,
    "electricity": 3,
    "housing": 4,
    "internet": 5,
    "mobile": 6,
}
_CATEGORY_SLOT_SPAN = {"gas": 2}  # how many slots a category may claim


def _series_slots(provs: Sequence[Provider]) -> dict[str, int]:
    """Service name → stable colour slot.

    Keyed by NAME rather than provider id on purpose: the same service at either
    property wears one colour, so a household filter never repaints the chart. The slot
    comes from the service's CATEGORY — a property of the entity, never of its spend —
    so re-sorting a table by amount can't change which colour a service wears.
    """
    slots: dict[str, int] = {}
    seen: dict[str, int] = {}
    for prov in sorted(
        provs, key=lambda p: (_CATEGORY_ORDER.get(p.category.value, 99), p.name)
    ):
        if prov.name in slots:
            continue
        cat = prov.category.value
        nth = seen.get(cat, 0)
        seen[cat] = nth + 1
        base = _CATEGORY_SLOT.get(cat)
        if base is None or nth >= _CATEGORY_SLOT_SPAN.get(cat, 1):
            # An unforeseen category, or more services in one than the palette budgets
            # for → the neutral «Інші» colour rather than a hue stolen from a neighbour.
            slots[prov.name] = charts.SLOT_COUNT
        else:
            slots[prov.name] = base + nth
    return slots


def _delta_note(total: Decimal, previous: Decimal | None, prev_cycle: str) -> str | None:
    """'▲ +8% до травня' — the month-over-month line under the grand total.

    None when there's nothing honest to compare against: no previous month, or it was
    empty. A percentage change off a zero base is undefined, not infinite — so we say
    nothing rather than print '+∞%'.

    Takes the CYCLE, not a pre-formatted label, because the two phrasings need different
    cases: «до» governs the genitive (до травня), «у» the locative (як у травні). One
    label for both produced «≈ як у червня».
    """
    if previous is None or previous <= 0 or total <= 0:
        return None
    pct = (total - previous) / previous * 100
    if abs(pct) < 1:
        return f"≈ як у {clock.format_cycle_locative(prev_cycle)}"
    arrow = "▲" if pct > 0 else "▼"
    return f"{arrow} {pct:+.0f}% до {clock.format_cycle_genitive(prev_cycle)}"


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
    provider: str | None = None,
) -> dict:
    """Total spend + breakdown by provider, month, or household, with a PNG chart.

    `household` (slug or address fragment) filters to one property; breakdown="household"
    splits the total across properties. `provider` (a name or category keyword like
    «газ»/«вода»/«інтернет») narrows to the matching provider(s) — «газ» catches both
    постачання + доставлення — and combines with `household` so «скільки за газ на
    Зеленій 151» answers only that property's gas. No household + provider/month
    breakdown = combined across both (the default, unchanged).

    An omitted `period` means THIS MONTH — and it has to mean that everywhere, not just
    in the caption. It used to be substituted only for the label (`period or
    current_cycle()`) while `_period_bounds(None)` left the query unbounded: the reply
    then showed the LIFETIME total under the heading «липень 2026», and (once the
    month-over-month line existed) compared that lifetime total against one real month —
    «▲ +610% до червня». Two fabricated numbers stated as fact. The model omitting
    `period` is not hypothetical: the arg is optional in the catalogue, and the
    dispatcher's own drop-unknown-args retry produces exactly this call.
    """
    period = period if period is not None else clock.current_cycle()
    start, end = _period_bounds(period)

    # provider → (name, household_id); household id → display name (env-seeded).
    provs = (await session.execute(select(Provider))).scalars().all()
    prov_name = {p.id: p.name for p in provs}
    prov_hid = {p.id: p.household_id for p in provs}
    hh_name = {h.id: h.name for h in (await session.execute(select(Household))).scalars()}

    want = await households.resolve(session, household) if household else None

    # Category/name filter: case-insensitive substring so «газ» → both gas providers,
    # «Газ (постачання)» → just one. None ⇒ no provider filter (all providers).
    prov_filter = (provider or "").strip()
    matched_pids: list[int] | None = None
    if prov_filter:
        needle = prov_filter.casefold()
        matched_pids = [p.id for p in provs if needle in p.name.casefold()]

    # Allowed provider ids = household filter ∩ provider filter (whichever are set).
    allowed: set[int] | None = None
    if want is not None:
        allowed = {p.id for p in provs if p.household_id == want.id}
    if matched_pids is not None:
        allowed = set(matched_pids) if allowed is None else allowed & set(matched_pids)

    conds: list[ColumnElement[bool]] = [Payment.provider_id.is_not(None)]
    if start is not None:
        conds.append(Payment.paid_at >= start)
    if end is not None:
        conds.append(Payment.paid_at < end)
    if allowed is not None:
        # empty set → [-1] matches nothing (filter named a provider/household with no tx)
        conds.append(Payment.provider_id.in_(list(allowed) or [-1]))

    payments = (await session.execute(select(Payment).where(*conds))).scalars().all()

    total = sum((p.amount_uah for p in payments), Decimal("0"))

    buckets: dict[str, Decimal] = {}
    month_label: str | None = None
    if breakdown == "month":
        for p in payments:
            key = clock.cycle_of(p.paid_at)
            buckets[key] = buckets.get(key, Decimal("0")) + p.amount_uah
        # A month nobody paid anything is a real, meaningful zero INSIDE the series —
        # dropping it would close the gap and make the trend lie about which month is
        # which. Two things are not real, though: a runway of empty months before the
        # journal begins, and a span too wide to read.
        cycles = _cycles_between(buckets, start, end)
        lost_data = False
        if len(cycles) > _MAX_MONTH_COLUMNS:
            # An open-ended «весь час» can span years: production holds one stray 2024-06
            # payment two years before the journal proper, which zero-filled into a
            # 26-column chart with 23 empties — hairline bars and no story. Keep the
            # readable recent window; note if that dropped a month that had money in it.
            lost_data = any(buckets.get(c) for c in cycles[:-_MAX_MONTH_COLUMNS])
            cycles = cycles[-_MAX_MONTH_COLUMNS:]
        # Leading empties are just "before the bot existed" — trim them (costs no data).
        cycles = _trim_leading_empty(cycles, buckets) or cycles
        buckets = {c: buckets.get(c, Decimal("0")) for c in cycles}
        if lost_data:
            # We dropped months that had payments, so the requested period's total is no
            # longer what's plotted. Recompute over exactly the columns shown and rename
            # the view after them — a caption must never claim a total the chart lacks.
            total = sum(buckets.values(), Decimal("0"))
            month_label = (
                f"{clock.format_cycle(cycles[0])} – {clock.format_cycle(cycles[-1])}"
            )
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

    # A by-month view is a TIME SERIES: it sorts chronologically, oldest first. Ranking
    # months cheapest-to-priciest (as every other breakdown is ranked) scrambles the very
    # axis that makes a trend readable. Cycle keys are zero-padded 'YYYY-MM', so
    # lexicographic order is chronological order.
    if breakdown == "month":
        ordered = sorted(buckets.items())
    else:
        ordered = sorted(buckets.items(), key=lambda kv: kv[1], reverse=True)
    items = [
        {
            "label": label,
            "total": str(amount),
            "share": (float(amount / total) if total else 0.0),
        }
        for label, amount in ordered
    ]

    period_key = period or clock.current_cycle()
    # Title: «<провайдер> · <житло> · <період>» — name whatever scopes are set.
    prov_scope: str | None = None
    if prov_filter:
        matched_names = [prov_name[i] for i in (matched_pids or [])]
        prov_scope = (
            matched_names[0]
            if len(matched_names) == 1
            else prov_filter[:1].upper() + prov_filter[1:]
        )
    scope_parts = [
        part for part in (prov_scope, want.name if want is not None else None) if part
    ]
    # `month_label` is set only when a by-month view had to clamp its window — then it,
    # not the requested period, is what the chart actually shows.
    label = " · ".join([*scope_parts, month_label or _period_label(period_key)])

    # Month-over-month: the same scope one cycle back. Only for a real "YYYY-MM" view —
    # comparing «весь час» or a season to "the previous one" isn't a thing anyone asked.
    delta_note: str | None = None
    if _is_cycle(period_key):
        prev_cycle = clock.shift_cycle(period_key, -1)
        prev_total = await _period_total(session, prev_cycle, allowed)
        delta_note = _delta_note(total, prev_total, prev_cycle)

    # Pick the FORM by the data's job. A by-month view's job is trend-over-time → a
    # column chart on a time axis. Everything else compares magnitudes across a handful
    # of named things → the table. Rendering months as table rows was why «по місяцях»
    # never actually showed a trend.
    # matplotlib is CPU-bound and the box has 2 shared cores: rendering on the event
    # loop would stall every other Telegram turn for the ~300-600ms it takes.
    if not items:
        chart_path = None
    elif breakdown == "month":
        ticks = _month_ticks([str(it["label"]) for it in items])
        chart_path = await _render(
            charts.render_trend,
            [
                (tick, Decimal(str(it["total"])))
                for tick, it in zip(ticks, items, strict=False)
            ],
            label,
        )
    else:
        # Only a by-provider row carries provider identity — a household row gets
        # slot=None (no chip), since a colour there would imply an identity it lacks.
        slots = _series_slots(provs) if breakdown == "provider" else {}
        rows: list[tuple[str, Decimal, float, int | None]] = [
            (
                str(it["label"]),
                Decimal(str(it["total"])),
                float(it.get("share") or 0.0),  # type: ignore[arg-type]
                slots.get(str(it["label"])) if breakdown == "provider" else None,
            )
            for it in items
        ]
        chart_path = await _render(charts.render_table, rows, label, total, delta_note)

    if not items:
        # Empty period (e.g. a month with no payments) must still answer — never hang.
        message = f"За {label} платежів не бачу — порожньо."
    elif chart_path:
        # Table image carries the breakdown → caption is just the period + total.
        message = _stats_caption(label, total)
        if delta_note:
            message += f"\n{delta_note}"
    else:
        # No image (matplotlib missing) → the text must carry the full breakdown.
        message = _stats_summary(label, total, items, breakdown)

    return {
        "period": period_key,
        "breakdown": breakdown,
        "household": want.slug if want is not None else None,
        "provider": prov_scope,
        "total": str(total),
        "items": items,
        "chart_path": chart_path,
        "message": message,
    }


def _is_cycle(period: str) -> bool:
    """True for a real 'YYYY-MM' month key (not 'all' / 'YYYY' / a season)."""
    parts = period.split("-")
    return (
        len(parts) == 2
        and len(parts[0]) == 4
        and parts[0].isdigit()
        and parts[1].isdigit()
        and 1 <= int(parts[1]) <= 12
    )


async def _period_total(
    session: AsyncSession, cycle: str, allowed: set[int] | None
) -> Decimal | None:
    """Total spend in `cycle` under the same scope, for the month-over-month delta.
    None when the month holds no payments at all (→ no honest % to show)."""
    start, end = _cycle_bounds(cycle)
    conds: list[ColumnElement[bool]] = [
        Payment.provider_id.is_not(None),
        Payment.paid_at >= start,
        Payment.paid_at < end,
    ]
    if allowed is not None:
        conds.append(Payment.provider_id.in_(list(allowed) or [-1]))
    rows = (await session.execute(select(Payment.amount_uah).where(*conds))).scalars()
    amounts = list(rows)
    return sum(amounts, Decimal("0")) if amounts else None


async def _render(fn: Callable[..., str], *args: Any) -> str | None:
    """Run a chart renderer off the event loop, and never let a rendering problem cost
    the user their answer: matplotlib missing, a font gap or a degenerate figure all
    degrade to a text reply (the caller falls back to `_stats_summary`)."""
    try:
        return await asyncio.to_thread(fn, *args)
    except Exception:  # noqa: BLE001 — a chart is a nicety; the numbers are the answer
        log.exception("chart render failed; falling back to text")
        return None


# --- dynamics (month-over-month trend) --------------------------------------

TREND_MODES = ("money", "provider", "volume")
_TREND_DEFAULT_MONTHS = 12
# Below two months there is no "dynamic" to show — one bar is a number, not a trend.
_TREND_MIN_MONTHS = 2


def _window(months: int, end_cycle: str | None = None) -> list[str]:
    """The last `months` cycles ending at `end_cycle` (default: this month), oldest
    first."""
    end = end_cycle or clock.current_cycle()
    return [clock.shift_cycle(end, -i) for i in range(months - 1, -1, -1)]


def _trim_leading_empty(cycles: list[str], totals: dict[str, Decimal]) -> list[str]:
    """Drop months before the first one with data.

    Leading zeros are just "before the bot existed" (the journal starts mid-2026) and
    would render as a runway of empty columns. Zeros INSIDE the range are kept — a
    month where nothing was paid is real information, not padding.
    """
    first = next((i for i, c in enumerate(cycles) if totals.get(c)), None)
    return [] if first is None else cycles[first:]


async def _payments_in_window(
    session: AsyncSession, cycles: list[str], allowed: set[int] | None
) -> list[Payment]:
    if not cycles:
        return []
    start, _ = _cycle_bounds(cycles[0])
    _, end = _cycle_bounds(cycles[-1])
    conds: list[ColumnElement[bool]] = [
        Payment.provider_id.is_not(None),
        Payment.paid_at >= start,
        Payment.paid_at < end,
    ]
    if allowed is not None:
        conds.append(Payment.provider_id.in_(list(allowed) or [-1]))
    return list((await session.execute(select(Payment).where(*conds))).scalars())


def _trend_stats_line(totals: list[Decimal], cycles: list[str]) -> str:
    """«сер. 4 210 ₴ · пік січень 6 890 ₴ · ▲ +8% до травня» — the numbers a bar chart
    can only approximate."""
    if not totals:
        return ""
    avg = sum(totals, Decimal("0")) / len(totals)
    peak_i = max(range(len(totals)), key=lambda i: totals[i])
    parts = [
        f"сер. {_fmt_uah(avg)} ₴",
        f"пік {clock.format_cycle_genitive(cycles[peak_i])} — "
        f"{_fmt_uah(totals[peak_i])} ₴",
    ]
    if len(totals) >= 2:
        note = _delta_note(totals[-1], totals[-2], cycles[-2])
        if note:
            parts.append(note)
    return " · ".join(parts)


async def get_stats_trend(
    session: AsyncSession,
    mode: str = "money",
    months: object = _TREND_DEFAULT_MONTHS,
    household: str | None = None,
    provider: str | None = None,
) -> dict:
    """Month-over-month DYNAMICS — how spending (or consumption) moves over time.

    Distinct from `get_stats`, which answers "what did one period cost, split by
    service". This answers "is it going up, and since when".

    `mode`:
      * "money"    — total ₴ per month (columns + average line);
      * "provider" — the same months split per service (stacked columns), so you can
        see WHICH service moved;
      * "volume"   — m³ per meter from the readings journal, as small multiples (gas
        runs ~40 m³/month and water ~3 m³ — one shared axis would flatten water, and a
        second y-axis is never the answer).

    Money and volume are deliberately separate charts: they are different measures and
    would need two scales on one plot.
    """
    mode = (mode or "money").strip().casefold()
    if mode not in TREND_MODES:
        mode = "money"
    span = _parse_months(months)

    provs = (await session.execute(select(Provider))).scalars().all()
    want = await households.resolve(session, household) if household else None
    allowed = _allowed_pids(provs, want, provider)
    scope = " · ".join(
        part
        for part in (_provider_scope(provs, provider), getattr(want, "name", None))
        if part
    )

    if mode == "volume":
        # `allowed` matters here as much as it does for money: «динаміка споживання води»
        # resolves scope to «Холодна вода» and TITLES the chart that — so dropping the
        # filter drew a gas panel under a heading that said water.
        return await _trend_volume(session, provs, span, want, scope, allowed)
    return await _trend_money(session, provs, span, allowed, scope, mode)


def _parse_months(months: object) -> int:
    """Clamp the window the model (or a button) asked for: 2..24 months. A wider window
    than the journal holds is harmless — empty leading months get trimmed."""
    try:
        span = int(str(months))
    except (TypeError, ValueError):
        span = _TREND_DEFAULT_MONTHS
    return max(_TREND_MIN_MONTHS, min(24, span))


def _allowed_pids(
    provs: Sequence[Provider], want: Any, provider: str | None
) -> set[int] | None:
    """Provider ids surviving the household ∩ provider filters (None = no filter)."""
    allowed: set[int] | None = None
    if want is not None:
        allowed = {p.id for p in provs if p.household_id == want.id}
    needle = (provider or "").strip().casefold()
    if needle:
        matched = {p.id for p in provs if needle in p.name.casefold()}
        allowed = matched if allowed is None else allowed & matched
    return allowed


def _provider_scope(provs: Sequence[Provider], provider: str | None) -> str | None:
    needle = (provider or "").strip().casefold()
    if not needle:
        return None
    names = [p.name for p in provs if needle in p.name.casefold()]
    return names[0] if len(names) == 1 else needle[:1].upper() + needle[1:]


async def _trend_money(
    session: AsyncSession,
    provs: Sequence[Provider],
    span: int,
    allowed: set[int] | None,
    scope: str,
    mode: str,
) -> dict:
    cycles = _window(span)
    payments = await _payments_in_window(session, cycles, allowed)

    totals: dict[str, Decimal] = {c: Decimal("0") for c in cycles}
    for p in payments:
        key = clock.cycle_of(p.paid_at)
        if key in totals:
            totals[key] += p.amount_uah
    cycles = _trim_leading_empty(cycles, totals)

    if len(cycles) < _TREND_MIN_MONTHS:
        return _trend_too_short(mode, scope)

    labels = _month_ticks(cycles)
    what = "витрати по послугах" if mode == "provider" else "динаміка витрат"
    title = " · ".join(part for part in (scope, what) if part)

    if mode == "provider":
        prov_name = {p.id: p.name for p in provs}
        slots = _series_slots(provs)
        per: dict[str, dict[str, Decimal]] = {}
        for p in payments:
            key = clock.cycle_of(p.paid_at)
            if key not in totals:
                continue
            name = prov_name.get(p.provider_id, "?") if p.provider_id else "?"
            per.setdefault(name, {})[key] = (
                per.setdefault(name, {}).get(key, Decimal("0")) + p.amount_uah
            )
        series = _fold_series(per, cycles, slots)
        chart = await _render(charts.render_stacked, labels, series, title)
    else:
        chart = await _render(
            charts.render_trend,
            [(lbl, totals[c]) for lbl, c in zip(labels, cycles, strict=False)],
            title,
        )

    ordered = [totals[c] for c in cycles]
    grand = sum(ordered, Decimal("0"))
    prefix = f"{scope} · " if scope else ""
    # Name the view: «💰 Гроші» and «🧾 По послугах» cover the same months and the same
    # total, so an identical caption made the two taps look like one broken button.
    view = "по послугах" if mode == "provider" else "по місяцях"
    head = f"📈 {prefix}{len(cycles)} міс. {view} — разом {_fmt_uah(grand)} ₴"
    message = f"{head}\n{_trend_stats_line(ordered, cycles)}".strip()
    return {
        "mode": mode,
        "months": cycles,
        "totals": [str(v) for v in ordered],
        "total": str(grand),
        "chart_path": chart,
        "message": message,
    }


def _fold_series(
    per: dict[str, dict[str, Decimal]],
    cycles: list[str],
    slots: dict[str, int],
) -> list[tuple[str, list[Decimal], int]]:
    """Per-service rows for the stacked chart, biggest first, folded to 6 + «Інші».

    Past the palette's slots a 7th hue would be indistinguishable under CVD from one
    already in use, so the tail folds into a single neutral «Інші» rather than
    generating colours. `slot` still comes from the service's stable display order, so
    a service keeps its colour whatever its rank this month.
    """
    # Keep as many series as the palette has validated slots (7) — not 6. The real setup
    # has exactly 7 distinct service names, so a hardcoded 6 folded one genuine service
    # into «Інші» when there was a colour sitting free for it.
    ranked = sorted(per.items(), key=lambda kv: sum(kv[1].values()), reverse=True)
    keep, tail = ranked[: charts.SLOT_COUNT], ranked[charts.SLOT_COUNT :]
    series = [
        (name, [buckets.get(c, Decimal("0")) for c in cycles], slots.get(name, 0))
        for name, buckets in keep
    ]
    if tail:
        merged = [
            sum((b.get(c, Decimal("0")) for _, b in tail), Decimal("0")) for c in cycles
        ]
        series.append(("Інші", merged, charts.SLOT_COUNT))  # → the neutral fold
    return series


def _trend_too_short(mode: str, scope: str) -> dict:
    """One month of history is a number, not a trend — say so plainly instead of
    rendering a single lonely bar and calling it dynamics."""
    what = "показників" if mode == "volume" else "витрат"
    return {
        "mode": mode,
        "months": [],
        "totals": [],
        "total": "0",
        "chart_path": None,
        "message": (
            f"Для динаміки {what} треба хоча б два місяці, а в мене поки менше. "
            "Ще трохи — і намалюю."
        ),
    }


async def _trend_volume(
    session: AsyncSession,
    provs: Sequence[Provider],
    span: int,
    want: Any,
    scope: str,
    allowed: set[int] | None = None,
) -> dict:
    """m³ per meter per month, from our own readings journal.

    Consumption is DERIVED from consecutive readings rather than read out of
    `MeterReading.consumption_delta`: that column is only filled when the delta
    validator had a previous value to hand, so on real data it is mostly NULL. The
    readings themselves are the source of truth — a meter is monotonic, so
    value(this month) − value(previous month) is the volume.
    """
    cycles = _window(span)
    slots = _series_slots(provs)
    hh_names = {
        h.id: h.name for h in (await session.execute(select(Household))).scalars()
    }
    meter_provs = [p for p in provs if p.meter_window is not None or p.static_reading]
    if want is not None:
        meter_provs = [p for p in meter_provs if p.household_id == want.id]
    if allowed is not None:  # the провайдер filter the scope/title already claims
        meter_provs = [p for p in meter_provs if p.id in allowed]
    # Two properties own identically-named meters (Газ (доставлення)) — without the
    # household the two panels are indistinguishable.
    shared = {
        p.name for p in meter_provs if [q.name for q in meter_provs].count(p.name) > 1
    }

    panels: list[tuple[str, list[str], list[Decimal], int]] = []
    for prov in sorted(meter_provs, key=_provider_order_key):
        readings = await _readings_by_cycle(session, prov.id)
        used = [c for c in cycles if c in readings]
        if len(used) < _TREND_MIN_MONTHS:
            continue
        drawn: list[str] = []
        values: list[Decimal] = []
        for prev, cur in zip(used, used[1:], strict=False):
            # ONLY consecutive months. With a gap (a month never filed) the difference
            # spans several months, and charting it against `cur` alone invents a spike:
            # a quarter of gas would be drawn as one monstrous month. We can't split it
            # across the missing months honestly, so we don't draw it at all.
            if clock.months_between(prev, cur) != 1:
                continue
            delta = readings[cur] - readings[prev]
            if delta < 0:  # a meter rolled over or a value was mis-entered — skip it
                continue
            drawn.append(cur)
            values.append(delta)
        labels = _month_ticks(drawn)
        if values:
            name = prov.name
            if name in shared and prov.household_id in hh_names:
                name = f"{name} · {hh_names[prov.household_id]}"
            panels.append((name, labels, values, slots.get(prov.name, 0)))

    if not panels:
        return _trend_too_short("volume", scope)

    title = " · ".join(part for part in (scope, "спожито по місяцях") if part)
    chart = await _render(charts.render_volume, panels, title)
    lines = [f"📈 {title or 'Спожито по місяцях'}"]
    for name, _labels, values, _slot in panels:
        # Quantize, then trim: «41.20» is read «сорок один кома двадцять» (i.e. 41.20,
        # not 41.2) — a trailing zero the screen doesn't need and the voice mispronounces.
        avg = (sum(values, Decimal("0")) / len(values)).quantize(Decimal("0.01"))
        lines.append(
            f"• {name}: сер. {meters.format_reading(avg)} м³/міс, "
            f"останнє {meters.format_reading(values[-1])} м³"
        )
    return {
        "mode": "volume",
        "months": cycles,
        "totals": [],
        "total": "0",
        "chart_path": chart,
        "message": "\n".join(lines),
    }


async def _readings_by_cycle(
    session: AsyncSession, provider_id: int
) -> dict[str, Decimal]:
    """cycle → the authoritative reading for that month. A `submitted` reading wins over
    a later un-filed re-photo of the same month (same rule as the journal)."""
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
                .order_by(MeterReading.created_at)
            )
        )
        .scalars()
        .all()
    )
    out: dict[str, Decimal] = {}
    for r in rows:
        if r.value is None:
            continue
        existing = out.get(r.cycle)
        if existing is None or r.status == MeterStatus.submitted:
            out[r.cycle] = r.value
    return out


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


async def _history_values(
    session: AsyncSession, provider_id: int, exclude_cycle: str | None = None
) -> list[Decimal]:
    """Validated/submitted readings for a provider, most-recent first.

    `exclude_cycle` drops readings of that month so a SECOND photo of the same meter in
    the same month is validated against the PREVIOUS month's reading — not against the
    earlier same-month photo (which would otherwise read as «0 спожито» or a bogus delta).
    Meters are cumulative: a month's use is always vs an earlier *different* cycle.
    """
    stmt = select(MeterReading).where(
        MeterReading.provider_id == provider_id,
        MeterReading.value.is_not(None),
        MeterReading.status.in_((MeterStatus.validated, MeterStatus.submitted)),
    )
    if exclude_cycle is not None:
        stmt = stmt.where(MeterReading.cycle != exclude_cycle)
    rows = (
        (await session.execute(stmt.order_by(MeterReading.created_at.desc())))
        .scalars()
        .all()
    )
    return [r.value for r in rows if r.value is not None]


async def _portal_baseline_value(
    session: AsyncSession, provider: Provider
) -> Decimal | None:
    """The infolviv portal's last filed value for this meter — the authoritative
    «previous» reading used to validate a fresh photo even when our local journal is
    empty (a clean DB, or a value filed straight on the portal). Without it a misread
    like 14.679 against a real 108.679 sails through as a «перший показник».

    Routed to the right counter by household account (the two gas counters share one
    login). Best-effort: any network/auth/parse failure → None, so OCR validation never
    blocks on the portal being reachable."""
    if provider.category.value not in ("gas", "water"):
        return None
    account: str | None = None
    if provider.household_id is not None:
        hh = await session.get(Household, provider.household_id)
        account = hh.infolviv_account_code if hh else None
    try:
        from dvoretskyi.agent.infolviv import reading_for_kind

        portal = await reading_for_kind(provider.category.value, account_code=account)
    except Exception:  # network/auth/parse — never let it sink a reading
        log.warning(
            "infolviv baseline lookup for %s failed", provider.name, exc_info=True
        )
        return None
    return portal.value if portal is not None else None


async def meter_hints(
    session: AsyncSession, providers: list[Provider]
) -> dict[str, Decimal]:
    """Per-kind anchor values (portal last filed, else local last reading) for the photo
    handler to feed into a SINGLE anchored OCR. Letting one read know «if water, prev was
    ~108; if gas, ~1890» fixes ambiguous-wheel misreads without a second OCR round."""
    out: dict[str, Decimal] = {}
    for prov in providers:
        anchor = await _portal_baseline_value(session, prov)
        if anchor is None:
            hist = await _history_values(session, prov.id)
            anchor = hist[0] if hist else None
        if anchor is not None:
            out[prov.category.value] = anchor
    return out


async def _supersede_pending(
    session: AsyncSession, provider_id: int, keep_id: int | None, cycle: str
) -> None:
    """Drop this meter's older un-filed readings **for this month** — we keep & submit
    only the freshest.

    A fresh photo of a meter replaces an earlier draft of the SAME meter in the SAME
    month, so the journal never accumulates stale duplicates (that confused the user:
    «виглядає як 3 фото»). Submitted readings are the permanent record — untouched.

    The `cycle` scope is load-bearing. Without it this hard-deleted every un-filed
    reading of the meter ever taken: since `INFOLV_SUBMIT_ENABLED` defaults off, the
    normal path leaves readings `validated`, so July's photo silently destroyed June's
    row and its archived photo. That collapsed the month-by-month journal to one line
    per meter, left `consumption` uncomputable, and disabled the backwards/spike guards
    (every reading became «Перший показник — узяв за відлік»).
    """
    rows = (
        (
            await session.execute(
                select(MeterReading).where(
                    MeterReading.provider_id == provider_id,
                    MeterReading.cycle == cycle,
                    MeterReading.id != keep_id,
                    MeterReading.status != MeterStatus.submitted,
                )
            )
        )
        .scalars()
        .all()
    )
    for r in rows:
        photo_store.remove(r.photo_ref)  # drop the superseded draft's archived photo
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

    # Compare against earlier MONTHS only — a re-shoot of this month isn't "consumption".
    history = await _history_values(session, prov.id, exclude_cycle=reading.cycle)
    portal_prev = await _portal_baseline_value(session, prov)
    anchor = portal_prev if portal_prev is not None else (history[0] if history else None)
    # OCR only if the caller didn't already hand us a read (the photo handler OCRs once,
    # anchored, and passes it). When we do OCR here, anchor it on the previous value in
    # the SAME pass — one round, not a blind read + a hinted re-read — so a rounded 0
    # isn't misread as 4 (108 → 148) yet the latency stays a single vision call.
    if read is None:
        hints = {prov.category.value: anchor} if anchor is not None else None
        read = await (vision or get_vision_provider()).read_meter(image_path, hints=hints)
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

    # Seed the validation baseline with the portal's authoritative last filed value when
    # it's higher than (or absent from) our local journal — so a backwards misread is
    # caught against what's actually on infolviv. Meters are monotonic, so the highest
    # known prior reading is the true «previous».
    if portal_prev is not None and (not history or portal_prev > history[0]):
        history = [portal_prev, *history]
    verdict = meters.validate(
        value,
        history,
        spike_k=settings.delta_spike_k,
        abs_cap=settings.delta_abs_cap,
    )
    reading.value = value
    reading.consumption_delta = verdict.consumption
    # Independent OCR reads disagreed → don't trust the number even if the delta looks
    # plausible (a 108→148 misread is a believable +40 the spike check can't catch).
    uncertain = not read.confident
    reading.status = MeterStatus.needs_confirm if uncertain else verdict.status
    await session.flush()
    # Keep a compressed copy so the user can pull this meter's photo back later. The
    # temp download (`image_path`, set above) is deleted by the caller, so only a
    # successful archive leaves a ref we can actually serve — on a miss, drop the ref
    # rather than dangle the soon-deleted temp path (which would later 404 as a phantom
    # «📸» that points at nothing).
    reading.photo_ref = photo_store.archive(image_path, reading.id)
    # A fresh reading supersedes an earlier un-filed draft of this meter THIS MONTH.
    # Earlier months are the journal — never collateral of a new photo.
    await _supersede_pending(session, prov.id, reading.id, reading.cycle)

    if not verdict.ok or uncertain:
        # A real validation failure (backwards/zero/spike) explains itself; an OCR
        # disagreement that otherwise looks fine gets its own «звір показник» reason.
        if not verdict.ok:
            message = verdict.reason
        else:
            alt = (
                f" Друге зчитування дало {read.alt_value}."
                if read.alt_value is not None
                else ""
            )
            message = (
                f"Не впевнений у показнику.{alt} Звір із лічильником: якщо там {value} — "
                "підтверди; інакше перефотографуй ближче (фронтально, при світлі)."
            )
        return {
            "ok": False,
            "reading_id": reading.id,
            "provider": prov.name,
            "kind": prov.category.value,
            "value": str(value),
            "status": reading.status.value,
            "consumption": (
                str(verdict.consumption) if verdict.consumption is not None else None
            ),
            "message": message,
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
        photo_store.remove(reading.photo_ref)  # drop its archived photo too
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
        photo_store.remove(reading.photo_ref)  # drop archived photos too
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
        # Meter readings are in m³ — name the unit, else «спожито 3.03» reads as «чого?»
        cons = (
            f" (спожито {portal.difference} м³)" if portal.difference is not None else ""
        )
        blocks = [
            f"🔢 {provider} — на порталі infolviv:\n• {period}: {portal.value} м³{cons}"
        ]
        drafts = [
            r for r in readings if r.get("status") in ("validated", "needs_confirm")
        ]
        if drafts:
            lines = "\n".join(
                f"• {clock.format_cycle(r['cycle'])}: "
                + (f"{r['value']} м³" if r.get("value") else "—")
                for r in drafts
            )
            blocks.append("📝 Збережено з фото (ще не подано на портал):\n" + lines)
        return "\n\n".join(blocks)
    # No portal record → show whatever local history we have.
    if readings:
        lines = "\n".join(
            f"• {clock.format_cycle(r['cycle'])}: "
            + (f"{r['value']} м³" if r.get("value") else "—")
            + (f" (спожито {r['consumption']} м³)" if r.get("consumption") else "")
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


async def _household_name(session: AsyncSession, provider: Provider) -> str | None:
    if provider.household_id is None:
        return None
    hh = await session.get(Household, provider.household_id)
    return hh.name if hh else None


async def get_meter_photo(
    session: AsyncSession,
    provider_name: str | None = None,
    cycle: str | None = None,
) -> dict:
    """Pull back the saved photo of a meter («витягни фото газу»).

    Returns the archived photo path + a caption naming the meter, household and value.
    Provider/cycle narrow it; otherwise the freshest archived photo wins. The bot sends
    the file — this tool only locates it. No photo on disk → ok=False with a hint.
    """
    stmt = select(MeterReading).where(MeterReading.photo_ref.is_not(None))
    if provider_name:
        prov = await _provider_by_name(session, provider_name)
        stmt = stmt.where(MeterReading.provider_id == prov.id)
    if cycle:
        stmt = stmt.where(MeterReading.cycle == _normalize_cycle(cycle))
    rows = (
        (await session.execute(stmt.order_by(MeterReading.created_at.desc())))
        .scalars()
        .all()
    )
    # The newest reading whose archived file still exists on disk.
    reading = next((r for r in rows if photo_store.exists(r.photo_ref)), None)
    if reading is None:
        scope = f" {provider_name}" if provider_name else ""
        return {
            "ok": False,
            "message": f"Фото лічильника{scope} не збереглося — скинь нове, відкладу.",
        }
    return await _photo_result(session, reading)


async def get_meter_photo_by_id(session: AsyncSession, reading_id: int) -> dict:
    """Pull back ONE specific reading's archived photo — the «📸 Фото» tap in the journal,
    where the exact reading is known. Gone from disk → ok=False with a hint."""
    reading = await session.get(MeterReading, reading_id)
    if reading is None or not photo_store.exists(reading.photo_ref):
        return {
            "ok": False,
            "message": "Це фото вже не збереглося — скинь нове, відкладу.",
        }
    return await _photo_result(session, reading)


async def _photo_result(session: AsyncSession, reading: MeterReading) -> dict:
    """Build the send-able photo dict (path + captions naming meter, household, value)."""
    owner = (
        await session.get(Provider, reading.provider_id)
        if reading.provider_id is not None
        else None
    )
    name = owner.name if owner else "Лічильник"
    hh = await _household_name(session, owner) if owner else None
    suffix = f" · {hh}" if hh else ""
    period = clock.format_cycle(reading.cycle) if reading.cycle else ""
    value = f" — {reading.value}" if reading.value is not None else ""
    when = f" ({period})" if period else ""
    caption = f"📸 {name}{suffix}{value}{when}"
    # HTML variant: the value rides in <code> so Telegram doesn't auto-link the digit run
    # «151/54 — 1888.140» as a phone number (same trick as the portal block's «рахунок»).
    val_html = (
        f" — <code>{html.escape(str(reading.value))}</code>"
        if reading.value is not None
        else ""
    )
    caption_html = f"📸 {html.escape(name + suffix)}{val_html}{html.escape(when)}"
    return {
        "ok": True,
        "provider": name,
        "household": hh,
        "photo_path": reading.photo_ref,
        "caption": caption,
        "caption_html": caption_html,
        "message": caption,
    }


# --- meter journal (L2): the month-by-month history with filing dates ------

_METER_EMOJI = {"water": "💧", "gas": "🔥"}

# Months of journal shown per meter before folding the tail into «…і ще N міс.». A year
# is what anyone actually reads, and it keeps the reply well inside Telegram's limits
# however long the journal grows.
_JOURNAL_MONTHS = 12


def _parse_limit(value: object, default: int) -> int:
    """A caller- or model-supplied row cap, clamped to something sane."""
    try:
        return max(1, min(60, int(str(value))))
    except (TypeError, ValueError):
        return default


def _meter_journal_message(sections: list[dict]) -> str:
    """Render the local journal newest-first: month → reading (consumption) → when it was
    filed (or «чернетка»), with a 📸 mark where the photo is still archived."""
    blocks: list[str] = []
    any_photo = False
    for sec in sections:
        if not sec["readings"]:
            continue
        label = f"{_METER_EMOJI.get(sec['category'], '🔢')} {sec['provider']}"
        if sec.get("household"):
            label += f" · {sec['household']}"
        lines = [label]
        for r in sec["readings"]:
            cons = f" (спожито {r['consumption']})" if r.get("consumption") else ""
            if r.get("submitted_at"):
                when = f"подано {r['submitted_at'].strftime('%d.%m')}"
            elif r["status"] == "needs_confirm":
                when = "чернетка, чекає підтвердження"
            else:
                when = "чернетка, ще не подано"
            mark = ""
            if r.get("has_photo"):
                mark = " 📸"
                any_photo = True
            lines.append(
                f"• {clock.format_cycle(r['cycle'])} — {r['value']}{cons} · {when}{mark}"
            )
        if sec.get("more"):
            lines.append(f"  …і ще {sec['more']} міс. — спитай, покажу.")
        blocks.append("\n".join(lines))
    if not blocks:
        return (
            "Поки що історії нема — журнал чистий. 🔢\n"
            "Кинь фото лічильника, і я вестиму помісячний журнал."
        )
    out = "📜 Історія показників:\n\n" + "\n\n".join(blocks)
    if any_photo:
        out += "\n\n📸 — фото збережено: напиши «витягни фото газу за <місяць>»."
    return out


async def get_meter_journal(
    session: AsyncSession,
    provider_name: str | None = None,
    limit: object = _JOURNAL_MONTHS,
) -> dict:
    """Month-by-month meter journal from **our own records** — the only place with the
    full history AND **when each reading was filed** (the infolviv portal returns just
    the latest factor). Covers every meter we track across both households (the secondary
    static gas included), newest-first, marking which months still have a saved photo.
    Optional `provider_name` narrows to one meter («історія газу»).

    `limit` caps the months shown PER METER (the rest are counted in «…і ще N місяців»).
    Without it the reply grew ~180 chars a month forever and crossed Telegram's 4096-char
    ceiling after roughly two years — at which point the send raised and the «📜 Історія»
    view was permanently broken, exactly when the history had become worth reading.
    """
    months = _parse_limit(limit, _JOURNAL_MONTHS)
    prov_stmt = select(Provider).where(Provider.meter_window.is_not(None))
    if provider_name:
        prov = await _provider_by_name(session, provider_name)
        prov_stmt = prov_stmt.where(Provider.id == prov.id)
    providers = list((await session.execute(prov_stmt)).scalars().all())
    providers.sort(key=_provider_order_key)  # gas, water, … home household first
    household_names = {
        h.id: h.name for h in (await session.execute(select(Household))).scalars()
    }

    sections: list[dict] = []
    for prov in providers:
        rows = (
            (
                await session.execute(
                    select(MeterReading)
                    .where(
                        MeterReading.provider_id == prov.id,
                        MeterReading.status.in_(
                            (
                                MeterStatus.submitted,
                                MeterStatus.validated,
                                MeterStatus.needs_confirm,
                            )
                        ),
                        MeterReading.value.is_not(None),
                    )
                    .order_by(MeterReading.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        # Group every reading by cycle (rows are newest-first, created_at desc), then emit
        # one line per month.
        by_cycle: dict[str, list[MeterReading]] = {}
        for r in rows:
            by_cycle.setdefault(r.cycle, []).append(r)
        readings = []
        shown = sorted(by_cycle, reverse=True)
        more = max(0, len(shown) - months)
        for cycle in shown[:months]:
            group = by_cycle[cycle]
            # The line shows the filed (submitted) reading when there is one — it's the
            # authoritative value + date; otherwise the freshest draft.
            display = next(
                (r for r in group if r.status is MeterStatus.submitted), group[0]
            )
            # The surviving photo may sit on a DIFFERENT row than `display` — e.g. a
            # re-photo draft taken after filing, or the filed row's own archive went
            # missing. Surface whichever row in this month still has a file on disk,
            # preferring `display`'s own, so a real photo is never hidden by the dedup.
            photo_row = (
                display
                if (display.photo_ref and photo_store.exists(display.photo_ref))
                else next(
                    (r for r in group if r.photo_ref and photo_store.exists(r.photo_ref)),
                    None,
                )
            )
            readings.append(
                {
                    "id": display.id,
                    "photo_id": photo_row.id if photo_row else None,
                    "cycle": cycle,
                    "value": str(display.value),
                    "consumption": (
                        str(display.consumption_delta)
                        if display.consumption_delta is not None
                        else None
                    ),
                    "submitted_at": (
                        clock.ensure_aware(display.submitted_at)
                        if display.submitted_at
                        else None
                    ),
                    "status": display.status.value,
                    "has_photo": photo_row is not None,
                }
            )
        hh_name = (
            household_names.get(prov.household_id)
            if prov.household_id is not None
            else None
        )
        sections.append(
            {
                "provider": prov.name,
                "household": hh_name,
                "category": prov.category.value,
                "readings": readings,
                "more": more,
            }
        )
    return {"sections": sections, "message": _meter_journal_message(sections)}


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
            "pay_label": f"🌐 Поповнити {fee_label} ₴",
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


# --- payment journal & plan ------------------------------------------------

_CATEGORY_EMOJI = {
    "water": "💧",
    "electricity": "💡",
    "gas": "🔥",
    "internet": "🌐",
    "housing": "🏠",
    "mobile": "📱",
}


def _payment_journal_message(sections: list[dict]) -> str:
    """Render the payment history newest-first: per provider (+ household), each payment
    as «<dd.mm.yyyy> — <сума> ₴». Mirror of the meter journal's shape and voice."""
    blocks: list[str] = []
    for sec in sections:
        if not sec["payments"]:
            continue
        label = f"{_CATEGORY_EMOJI.get(sec['category'], '💸')} {sec['provider']}"
        if sec.get("household"):
            label += f" · {sec['household']}"
        lines = [label]
        lines += [f"• {p['date']} — {p['amount']} ₴" for p in sec["payments"]]
        if sec.get("more"):
            lines.append(f"  …та ще {sec['more']} раніше")
        blocks.append("\n".join(lines))
    if not blocks:
        return (
            "Платежів поки не бачу — історія порожня. 💸\n"
            "Щойно monobank повідомить про оплату, занесу її сюди з датою."
        )
    return "💸 Історія платежів:\n\n" + "\n\n".join(blocks)


async def get_payment_journal(
    session: AsyncSession,
    provider_name: str | None = None,
    household: str | None = None,
    period: str | None = None,
) -> dict:
    """Per-payment history WITH DATES, newest-first, grouped by provider (+ household).

    The only view with the actual PAYMENT DATE of each transaction — `get_stats` gives
    totals, this gives the dated timeline. Answers «коли я платив за газ», «коли востаннє
    платив за світло», «покажи платежі по <житлу>». `provider_name` narrows to one service
    (substring → «газ» catches both gas providers); `household` filters to one property;
    `period` ("YYYY"/"YYYY-MM"/season) limits the range."""
    start, end = _period_bounds(period)

    provs = list((await session.execute(select(Provider))).scalars().all())
    provs.sort(key=_provider_order_key)  # gas, water, electricity, … home first
    household_names = {
        h.id: h.name for h in (await session.execute(select(Household))).scalars()
    }
    want = await households.resolve(session, household) if household else None
    needle = (provider_name or "").strip().casefold()

    per_provider = 6  # cap lines per provider so the journal stays readable
    sections: list[dict] = []
    for prov in provs:
        if want is not None and prov.household_id != want.id:
            continue
        if needle and needle not in prov.name.casefold():
            continue
        conds: list[ColumnElement[bool]] = [Payment.provider_id == prov.id]
        if start is not None:
            conds.append(Payment.paid_at >= start)
        if end is not None:
            conds.append(Payment.paid_at < end)
        rows = (
            (
                await session.execute(
                    select(Payment).where(*conds).order_by(Payment.paid_at.desc())
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            continue
        payments = [
            {
                "date": clock.ensure_aware(p.paid_at).strftime("%d.%m.%Y"),
                "amount": _fmt_uah(p.amount_uah),
                "cycle": clock.cycle_of(clock.ensure_aware(p.paid_at)),
            }
            for p in rows[:per_provider]
        ]
        sections.append(
            {
                "provider": prov.name,
                "household": (
                    household_names.get(prov.household_id) if prov.household_id else None
                ),
                "category": prov.category.value,
                "payments": payments,
                "more": max(0, len(rows) - per_provider),
            }
        )
    return {"sections": sections, "message": _payment_journal_message(sections)}


def _payment_plan_message(rows: list[dict], autopay: list[dict]) -> str:
    """Render the monthly payment plan: per scheduled provider — by which day, how much
    (if known) and through which service; mobile's autopay is noted separately."""
    if not rows and not autopay:
        return "Поки нема за що платити за планом — жодної послуги з датою оплати."
    blocks: list[str] = ["🗓 Як і коли платимо щомісяця:"]
    for r in rows:
        head = f"{_CATEGORY_EMOJI.get(r['category'], '💸')} {r['provider']}"
        if r.get("household"):
            head += f" · {r['household']}"
        amount = f" ≈{r['expected_amount']} ₴," if r.get("expected_amount") else ""
        blocks.append(f"{head}\n• до {r['due_day']}-го,{amount} через {r['method']}")
    for a in autopay:
        head = f"{_CATEGORY_EMOJI.get(a['category'], '📱')} {a['provider']}"
        blocks.append(
            f"{head}\n• автосписанням monobank {a['autopay_day']}-го — "
            "робити нічого не треба"
        )
    tail = "\n\nПосилання на оплату — кнопками нижче." if rows else ""
    return "\n\n".join(blocks) + tail


async def get_payment_plan(session: AsyncSession, household: str | None = None) -> dict:
    """The monthly payment plan: for each scheduled service — WHEN (due day), HOW MUCH
    (if a typical amount is known) and THROUGH WHICH SERVICE it's paid (monobank
    «Комуналка» / застосунок ДАХ / Portmone for Gigabit+), plus the relevant pay links.
    Answers «коли і за що я плачу», «як і де платити», «що по оплатах цього місяця».
    `household` filters to one property; mobile autopay is listed as a no-action note.
    `links` (deduped by url) are rendered as tappable buttons by the bot layer."""
    from dvoretskyi.agent.balance import pay_link_for, pay_method_label

    want = await households.resolve(session, household) if household else None
    household_names = {
        h.id: h.name for h in (await session.execute(select(Household))).scalars()
    }
    provs = list((await session.execute(select(Provider))).scalars().all())
    provs.sort(key=_provider_order_key)  # gas, water, electricity, … home first

    rows: list[dict] = []
    autopay: list[dict] = []
    links: list[dict] = []
    seen_urls: set[str] = set()
    autopay_day = get_settings().mobile_autopay_day

    for prov in provs:
        if want is not None and prov.household_id != want.id:
            continue
        hh = household_names.get(prov.household_id) if prov.household_id else None
        if prov.category is Category.mobile and prov.due_day is None:
            autopay.append(
                {
                    "provider": prov.name,
                    "category": prov.category.value,
                    "autopay_day": autopay_day,
                }
            )
            continue
        if prov.due_day is None:
            continue  # no scheduled payment (e.g. the unoccupied flat's providers)
        rows.append(
            {
                "provider": prov.name,
                "household": hh,
                "category": prov.category.value,
                "due_day": prov.due_day,
                "expected_amount": (
                    _fmt_uah(prov.expected_amount)
                    if prov.expected_amount is not None
                    else None
                ),
                "method": pay_method_label(prov),
            }
        )
        url, label = pay_link_for(prov)
        if url and label and url not in seen_urls:
            seen_urls.add(url)
            links.append({"url": url, "label": label})

    return {
        "rows": rows,
        "autopay": autopay,
        "links": links,
        "message": _payment_plan_message(rows, autopay),
    }


Tool = Callable[..., Awaitable[dict[str, Any]]]

TOOLS: dict[str, Tool] = {
    "get_unpaid": get_unpaid,
    "get_stats": get_stats,
    "get_stats_trend": get_stats_trend,
    "get_payment_journal": get_payment_journal,
    "get_payment_plan": get_payment_plan,
    "log_payment_manual": log_payment_manual,
    "categorize_payment": categorize_payment,
    "snooze_reminder": snooze_reminder,
    # Meters (L2). submit_meter_reading needs an image_path (supplied by the photo
    # handler, not the LLM); confirm/history are LLM-callable.
    "submit_meter_reading": submit_meter_reading,
    "confirm_meter_reading": confirm_meter_reading,
    "delete_meter_reading": delete_meter_reading,
    "get_meter_history": get_meter_history,
    "get_meter_journal": get_meter_journal,
    "get_meter_photo": get_meter_photo,
    "get_provider_balance": get_provider_balance,
}
