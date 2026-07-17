"""Delta validation — the trust layer that catches OCR misreads before submission.

Pure logic over a new reading + the provider's reading history (most-recent-first
list of validated Decimals). No DB, no I/O — easy to test exhaustively. The butler
asks the user to confirm anything that looks off (`needs_confirm`) rather than
silently submitting a wrong number.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from dvoretskyi.db.models import MeterStatus


def format_reading(value: Decimal) -> str:
    """A meter value as a human reads it: '1888.140' → '1888.14', '90.000' → '90'.

    Readings are stored in a Numeric(14,3) column, so SQLAlchemy hands them back padded
    to three decimals whatever the meter's own precision. That padding is a storage
    artifact: «показник 1888.140» reads as noise, and the voice layer would dutifully
    spell out each trailing zero.
    """
    trimmed = value.normalize()
    # normalize() renders a whole number in exponent form (90.000 → 9E+1) — undo that.
    return f"{trimmed:f}"


def days_until_month_end(now: datetime) -> int:
    """Whole days from `now` to the last day of its month (0 on the last day).

    The month length is taken from the calendar (handles 28/29/30/31), never hardcoded.
    """
    last_day = calendar.monthrange(now.year, now.month)[1]
    return last_day - now.day


def window_open(meter_window: int, now: datetime) -> bool:
    """Is the meter-reading window open now? `meter_window` is a LEAD TIME — how many
    days before month end to start nudging (not a day-of-month). Open once we're within
    that many days of the last day of the month (inclusive of the last day)."""
    return days_until_month_end(now) <= meter_window


def submit_now(
    now: datetime,
    *,
    attempt: int,
    submit_from_day: int = 28,
    max_attempts: int = 3,
) -> bool:
    """Decide whether to file a reading right now, or hold it for end of month.

    A reading is "current enough" to file from `submit_from_day` (28th) to month end —
    in that window we submit on the user's approval. Before it we hold, BUT if the user
    insists we relent: the `attempt`-th «подай раніше» tap submits once it reaches
    `max_attempts` (resist twice, file on the 3rd). The submission window is always open
    on the 28th+ regardless of month length (28/29/30/31)."""
    if now.day >= submit_from_day:
        return True
    return attempt >= max_attempts


@dataclass
class Validation:
    ok: bool  # True → safe to treat as validated; False → needs_confirm
    status: MeterStatus
    consumption: Decimal | None  # value - previous validated value
    reason: str  # butler-voice explanation (Ukrainian) when not ok; else short note


def _median(values: list[Decimal]) -> Decimal:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def validate(
    new_value: Decimal,
    history: list[Decimal],
    *,
    spike_k: int = 3,
    abs_cap: Decimal = Decimal("1000"),
) -> Validation:
    """Classify `new_value` against `history` (most-recent first).

    Rules:
    - no history → baseline, accept (consumption unknown);
    - value < previous → rollover-plausible? accept : needs_confirm;
    - consumption == 0 → needs_confirm (a live meter rarely shows zero use);
    - consumption > max(abs_cap, spike_k × median(consumption history)) → needs_confirm;
    - otherwise → validated.
    """
    if not history:
        return Validation(
            ok=True,
            status=MeterStatus.validated,
            consumption=None,
            reason="Перший показник — узяв за відлік.",
        )

    previous = history[0]
    consumption = new_value - previous

    if new_value < previous:
        # A real meter only goes up — unless it physically rolled over its max. We
        # can't know the dial count here, so flag it: cheaper to ask than to submit
        # a misread (e.g. a dropped leading digit).
        return Validation(
            ok=False,
            status=MeterStatus.needs_confirm,
            consumption=consumption,
            reason=(
                f"Новий показник {new_value} менший за попередній {previous}. "
                "Помилка зчитування чи лічильник перекрутився? "
                "Підтвердь або перефотографуй."
            ),
        )

    if consumption == 0:
        return Validation(
            ok=False,
            status=MeterStatus.needs_confirm,
            consumption=consumption,
            reason=(
                "Нуль споживання — точно нічого не намотало? "
                "Підтвердь або перефотографуй."
            ),
        )

    # Spike check against typical consumption.
    deltas = [a - b for a, b in zip(history, history[1:], strict=False) if a >= b]
    if deltas:
        typical = _median(deltas)
        threshold = max(abs_cap, spike_k * typical)
        if consumption > threshold:
            return Validation(
                ok=False,
                status=MeterStatus.needs_confirm,
                consumption=consumption,
                reason=(
                    f"Стрибок: +{consumption}, а зазвичай ~{typical}. "
                    "Зайва цифра в зчитуванні? Підтвердь або перефотографуй."
                ),
            )

    return Validation(
        ok=True,
        status=MeterStatus.validated,
        consumption=consumption,
        reason=f"Намотало +{consumption} — у межах звичного.",
    )
