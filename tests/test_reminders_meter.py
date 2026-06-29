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

    async def send(chat_id: int, text: str, **kw) -> None:
        sent.append((chat_id, text))

    pending = await run_meter_nudges(send, now=_at(2026, 6, 28))
    assert pending and sent
    assert any("газу" in text for _, text in sent)

    # A NudgeLog(kind=meter) was recorded → a second run the same day is suppressed.
    again = await run_meter_nudges(send, now=_at(2026, 6, 28))
    assert all(p.provider_name != "Газ (постачання)" for p in again)


async def test_meter_nudge_broadcasts_photo_to_family_static_stays_owner(
    session, households, providers, monkeypatch
):
    """Photo «кинь фото» nudges reach the whole allowlist (anyone may submit a reading);
    the static-meter approve tap is single-actor → owner only."""
    from decimal import Decimal

    from dvoretskyi.config import Settings
    from dvoretskyi.db.models import Category, PayChannel, Provider
    from dvoretskyi.reminders import engine as eng

    sec_gas = Provider(
        name="Газ (доставлення)",
        category=Category.gas,
        pay_channel=PayChannel.mono_communal,
        household_id=households["secondary"].id,
        meter_window=3,
        meter_decimals=2,
        static_reading=Decimal("7952.81"),
    )
    session.add(sec_gas)
    await session.commit()

    s = Settings(telegram_allowed_user_id=111, telegram_extra_allowed_user_ids={222, 333})
    monkeypatch.setattr(eng, "get_settings", lambda: s)

    sent: list[tuple[int, int | None]] = []  # (chat_id, approve_reading_id)

    async def send(chat_id, text, approve_reading_id=None, **kw):
        sent.append((chat_id, approve_reading_id))

    await run_meter_nudges(send, now=_at(2026, 6, 28))

    photo_recipients = {chat_id for chat_id, rid in sent if rid is None}
    static_recipients = {chat_id for chat_id, rid in sent if rid is not None}
    assert photo_recipients == {111, 222, 333}  # owner + family
    assert static_recipients == {111}  # owner only


async def test_static_meter_nudge_offers_one_tap_file(session, households, providers):
    """The unoccupied property's gas meter is a fixed value: the nudge stages a validated
    reading and offers the «📤 Подати» approve tap instead of asking for a photo."""
    from dvoretskyi.db.models import Category, PayChannel, Provider

    sec_gas = Provider(
        name="Газ (доставлення)",
        category=Category.gas,
        pay_channel=PayChannel.mono_communal,
        household_id=households["secondary"].id,
        meter_window=3,
        meter_decimals=2,
        static_reading=Decimal("7952.81"),
    )
    session.add(sec_gas)
    await session.commit()

    sent: list[tuple[str, int | None]] = []

    async def send(chat_id, text, approve_reading_id=None, **kw):
        sent.append((text, approve_reading_id))

    pending = await run_meter_nudges(send, now=_at(2026, 6, 28))
    static = next(p for p in pending if p.provider_id == sec_gas.id)
    assert "7952.81" in (static.static_value or "")
    assert static.reading_id is not None
    # The message names the fixed value and does NOT ask for a photo.
    assert "7952.81" in static.message() and "фото" not in static.message()
    # A validated reading was staged for the cycle, ready for the approve tap.
    staged = await session.get(MeterReading, static.reading_id)
    assert staged.value == Decimal("7952.81") and staged.status is MeterStatus.validated
    # The nudge carried the approve_reading_id so the bot renders the file tap.
    assert any(rid == static.reading_id for _, rid in sent)
