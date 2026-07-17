from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from dvoretskyi import clock
from dvoretskyi.agent.meters import submit_now, validate
from dvoretskyi.config import get_settings
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


def test_default_abs_cap_catches_a_leading_digit_misread_on_a_real_gas_meter():
    """The shipped default must actually catch the misread it exists to catch.

    Real gas readings run ~2.5 m³/month (1888.14 → 1890.68). `abs_cap` is a FLOOR on the
    spike threshold, so at its old default of 1000 the gate was max(1000, 3×2.5) = 1000
    — i.e. dead: a misread of the leading digit (1890 → 2890, +999 m³ of gas in a month)
    filed as `validated` without a question. Pin the default so it can't drift back.
    """
    st = get_settings()
    history = [D("1890.68"), D("1888.14"), D("1885.60"), D("1883.00")]  # ~2.5/month

    misread = validate(
        D("2890.00"), history, spike_k=st.delta_spike_k, abs_cap=st.delta_abs_cap
    )
    assert not misread.ok and misread.status is MeterStatus.needs_confirm

    normal = validate(
        D("1893.28"), history, spike_k=st.delta_spike_k, abs_cap=st.delta_abs_cap
    )
    assert normal.ok and normal.status is MeterStatus.validated


# --- get_stats period parsing: seasons + Ukrainian labels --------------------


def test_period_bounds_and_label_for_season_and_year():
    from datetime import datetime

    from dvoretskyi.agent.tools import _period_bounds, _period_label

    # «зима 2026» = Dec 2025 → Mar 2026 (3 meteorological months across the year edge).
    start, end = _period_bounds("зима 2026")
    assert start == datetime(2025, 12, 1, tzinfo=clock.KYIV)
    assert end == datetime(2026, 3, 1, tzinfo=clock.KYIV)
    assert _period_label("зима 2026") == "зима 2026"

    # «літо» with no year → current year, Jun→Sep.
    s2, e2 = _period_bounds("summer-2026")
    assert (s2.month, e2.month) == (6, 9)
    assert _period_label("2026") == "2026 рік"
    assert _period_label("all") == "весь час"
    assert _period_label("2026-05") == "травень 2026"


def test_relative_period_window_is_understood():
    """«за пів року» / «останні 6 місяців» / «6m» are a real range, not a crash.

    The catalog used to offer no way to say "a rolling window", so the model invented
    free text that reached int() and killed the whole turn.
    """
    from dvoretskyi.agent.tools import _period_bounds, _period_label

    for period in ("6m", "пів року", "останні 6 місяців"):
        start, end = _period_bounds(period)
        assert start is not None and end is not None
        # The window ends with the current month included and spans 6 months.
        assert clock.cycle_of(start) == clock.shift_cycle(clock.current_cycle(), -5)
        assert _period_label(period) == "останні 6 міс."


def test_unparseable_period_raises_tool_error_not_value_error():
    """A period the model invents must cost a sentence, not the whole turn: ToolError is
    surfaced to the user by the dispatcher; a ValueError would propagate and kill it."""
    import pytest

    from dvoretskyi.agent.tools import ToolError, _period_bounds

    for junk in ("останній квартал", "2026-13", "вчора", "2026-“"):
        with pytest.raises(ToolError):
            _period_bounds(junk)
