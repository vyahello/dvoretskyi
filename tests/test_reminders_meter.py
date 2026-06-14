from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from dvoretskyi import clock
from dvoretskyi.db.models import MeterReading, MeterStatus, NudgeKind, NudgeLog
from dvoretskyi.reminders.engine import (
    compute_pending_meter_nudges,
    run_meter_nudges,
)

# Fixture gas & water both have meter_window=3 → nudge over the final 3 days of any
# month (the last day + the two before it), regardless of month length.


def _at(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 9, 0, tzinfo=clock.KYIV)


async def test_fires_three_days_before_end(session, providers):
    # June has 30 days → window is the 27th..30th. Day 27 (3 days left) fires.
    pending = await compute_pending_meter_nudges(session, now=_at(2026, 6, 27))
    names = {p.provider_name for p in pending}
    assert {"Газ (постачання)", "Холодна вода"} <= names
    gas = next(p for p in pending if p.provider_name == "Газ (постачання)")
    assert gas.days_left == 3
    assert "газу" in gas.message() and "фото" in gas.message()


async def test_fires_on_last_day(session, providers):
    pending = await compute_pending_meter_nudges(session, now=_at(2026, 6, 30))
    gas = next(p for p in pending if p.provider_name == "Газ (постачання)")
    assert gas.days_left == 0
    assert "останній день" in gas.message()


async def test_not_earlier_than_window(session, providers):
    # Day 26 of June → 4 days left → outside the 3-day window.
    assert await compute_pending_meter_nudges(session, now=_at(2026, 6, 26)) == []
    # mid-month, definitely nothing
    assert await compute_pending_meter_nudges(session, now=_at(2026, 6, 12)) == []


async def test_february_non_leap_28(session, providers):
    # 2026 is not a leap year → Feb has 28 days. Window = 25th..28th (days_left 3..0).
    assert await compute_pending_meter_nudges(session, now=_at(2026, 2, 24)) == []
    fired = await compute_pending_meter_nudges(session, now=_at(2026, 2, 25))
    assert {p.provider_name for p in fired} >= {"Газ (постачання)"}
    last = await compute_pending_meter_nudges(session, now=_at(2026, 2, 28))
    gas = next(p for p in last if p.provider_name == "Газ (постачання)")
    assert gas.days_left == 0


async def test_february_leap_29(session, providers):
    # 2028 is a leap year → Feb has 29 days. The 29th is the last day.
    assert await compute_pending_meter_nudges(session, now=_at(2028, 2, 25)) == []
    fired = await compute_pending_meter_nudges(session, now=_at(2028, 2, 26))
    assert {p.provider_name for p in fired} >= {"Газ (постачання)"}
    last = await compute_pending_meter_nudges(session, now=_at(2028, 2, 29))
    gas = next(p for p in last if p.provider_name == "Газ (постачання)")
    assert gas.days_left == 0


async def test_thirty_one_day_month(session, providers):
    # July has 31 days → window is 28th..31st. Day 28 (3 left) fires; 27 does not.
    assert await compute_pending_meter_nudges(session, now=_at(2026, 7, 27)) == []
    fired = await compute_pending_meter_nudges(session, now=_at(2026, 7, 28))
    assert {"Газ (постачання)", "Холодна вода"} <= {p.provider_name for p in fired}


async def test_suppressed_once_reading_submitted(session, providers):
    gas = providers["Газ (постачання)"]
    session.add(
        MeterReading(
            provider_id=gas.id,
            cycle="2026-06",
            value=Decimal("1000"),
            status=MeterStatus.submitted,
            created_at=_at(2026, 6, 27),
        )
    )
    await session.commit()
    pending = await compute_pending_meter_nudges(session, now=_at(2026, 6, 28))
    assert all(p.provider_name != "Газ (постачання)" for p in pending)


async def test_suppressed_when_snoozed(session, providers):
    gas = providers["Газ (постачання)"]
    session.add(
        NudgeLog(
            provider_id=gas.id,
            cycle="2026-06",
            kind=NudgeKind.meter,
            nudged_at=_at(2026, 6, 27),
            snoozed_until=_at(2026, 6, 28) + timedelta(days=2),
        )
    )
    await session.commit()
    pending = await compute_pending_meter_nudges(session, now=_at(2026, 6, 28))
    assert all(p.provider_name != "Газ (постачання)" for p in pending)


async def test_run_meter_nudges_sends_and_logs(session, providers):
    sent: list[tuple[int, str]] = []

    async def send(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    pending = await run_meter_nudges(send, now=_at(2026, 6, 28))
    assert pending and sent
    assert any("газу" in text for _, text in sent)

    # A NudgeLog(kind=meter) was recorded → a second run the same day is suppressed.
    again = await run_meter_nudges(send, now=_at(2026, 6, 28))
    assert all(p.provider_name != "Газ (постачання)" for p in again)
