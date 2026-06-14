"""Delta validation — the trust layer that catches OCR misreads before submission.

Pure logic over a new reading + the provider's reading history (most-recent-first
list of validated Decimals). No DB, no I/O — easy to test exhaustively. The butler
asks the user to confirm anything that looks off (`needs_confirm`) rather than
silently submitting a wrong number.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from dvoretskyi.db.models import MeterStatus

METER_WINDOW_SPAN = 3  # nudge/route window: meter_window-SPAN … meter_window (inclusive)


def window_open(meter_window_day: int, today: int, span: int = METER_WINDOW_SPAN) -> bool:
    """Is `today` in a meter's submission window? gas≤5 → days 2..5; water 25 → 22..25."""
    return meter_window_day - span <= today <= meter_window_day


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
