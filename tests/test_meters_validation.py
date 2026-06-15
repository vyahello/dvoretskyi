from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from dvoretskyi import clock
from dvoretskyi.agent.meters import submit_now, validate
from dvoretskyi.db.models import MeterStatus


def _at(day: int, *, month: int = 6, year: int = 2026) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=clock.KYIV)


# --- submit_now: file from the 28th; before it, relent only on the 3rd insistence ---


def test_in_window_submits_on_first_approval():
    # On/after the 28th, the very first approval (attempt 1) files immediately.
    assert submit_now(_at(28), attempt=1) is True
    assert submit_now(_at(30), attempt=1) is True


def test_before_window_holds_until_third_insistence():
    assert submit_now(_at(10), attempt=1) is False  # «подай раніше» #1 → ні
    assert submit_now(_at(10), attempt=2) is False  # #2 → ні
    assert submit_now(_at(10), attempt=3) is True  # #3 → подаю


def test_window_start_is_28_regardless_of_month_length():
    # 28th opens the window in a 28-day Feb, a 30-day month, and a 31-day month alike.
    assert submit_now(_at(28, month=2), attempt=1) is True  # Feb 2026 (28 days)
    assert submit_now(_at(28, month=7), attempt=1) is True  # July (31 days)
    assert submit_now(_at(27, month=7), attempt=1) is False  # 27th still holds


def D(x: str | int) -> Decimal:
    return Decimal(str(x))


def test_baseline_no_history_accepts():
    v = validate(D(100), [])
    assert v.ok and v.status is MeterStatus.validated
    assert v.consumption is None


def test_normal_consumption_validated():
    # history most-recent-first: last=1000, before=950, before=900 → typical ~50
    v = validate(D(1060), [D(1000), D(950), D(900)])
    assert v.ok and v.status is MeterStatus.validated
    assert v.consumption == D(60)


def test_backwards_needs_confirm():
    v = validate(D(900), [D(1000), D(950)])
    assert not v.ok and v.status is MeterStatus.needs_confirm
    assert v.consumption == D(-100)
    assert "менший" in v.reason


def test_rollover_below_previous_is_flagged_not_silently_accepted():
    # We can't know the dial count, so a wrap still asks (cheaper than a misread).
    v = validate(D(5), [D(99990)])
    assert not v.ok and v.status is MeterStatus.needs_confirm


def test_zero_consumption_needs_confirm():
    v = validate(D(1000), [D(1000), D(950)])
    assert not v.ok and v.status is MeterStatus.needs_confirm
    assert v.consumption == D(0)
    assert "нуль" in v.reason.lower()


def test_spike_above_k_times_median_needs_confirm():
    # typical deltas ~50; an extra digit makes +6000 ≫ max(abs_cap, 3×50)
    v = validate(D(7000), [D(1000), D(950), D(900), D(850)], abs_cap=D(1000))
    assert not v.ok and v.status is MeterStatus.needs_confirm
    assert "стрибок" in v.reason.lower()


def test_spike_under_abs_cap_not_flagged():
    # A modest jump above median but below the absolute cap is allowed through.
    v = validate(D(1300), [D(1000), D(950), D(900)], spike_k=3, abs_cap=D(1000))
    assert v.ok and v.status is MeterStatus.validated
    assert v.consumption == D(300)
