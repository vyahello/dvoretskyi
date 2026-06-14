from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from komunalka import clock
from komunalka.db.models import MeterReading, MeterStatus, NudgeKind, NudgeLog
from komunalka.reminders.engine import (
    compute_pending_meter_nudges,
    run_meter_nudges,
)


def _at(day: int) -> datetime:
    return datetime(2026, 6, day, 9, 0, tzinfo=clock.KYIV)


async def test_gas_nudge_fires_in_window(session, providers):
    # Gas meter_window = 5 → window days 2..5. Day 4 with no reading → nudge.
    pending = await compute_pending_meter_nudges(session, now=_at(4))
    names = {p.provider_name for p in pending}
    assert "Газ (постачання)" in names
    assert "Холодна вода" not in names  # water window is 22..25
    gas = next(p for p in pending if p.provider_name == "Газ (постачання)")
    assert "газу" in gas.message() and "фото" in gas.message()


async def test_no_nudge_outside_window(session, providers):
    assert await compute_pending_meter_nudges(session, now=_at(12)) == []


async def test_nudge_suppressed_once_reading_submitted(session, providers):
    gas = providers["Газ (постачання)"]
    session.add(
        MeterReading(
            provider_id=gas.id,
            cycle="2026-06",
            value=Decimal("1000"),
            status=MeterStatus.submitted,
            created_at=_at(3),
        )
    )
    await session.commit()
    pending = await compute_pending_meter_nudges(session, now=_at(4))
    assert all(p.provider_name != "Газ (постачання)" for p in pending)


async def test_nudge_suppressed_when_snoozed(session, providers):
    gas = providers["Газ (постачання)"]
    session.add(
        NudgeLog(
            provider_id=gas.id,
            cycle="2026-06",
            kind=NudgeKind.meter,
            nudged_at=_at(3),
            snoozed_until=_at(4) + timedelta(days=2),
        )
    )
    await session.commit()
    pending = await compute_pending_meter_nudges(session, now=_at(4))
    assert all(p.provider_name != "Газ (постачання)" for p in pending)


async def test_run_meter_nudges_sends_and_logs(session, providers):
    sent: list[tuple[int, str]] = []

    async def send(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    pending = await run_meter_nudges(send, now=_at(4))
    assert pending and sent
    assert any("газу" in text for _, text in sent)

    # A NudgeLog(kind=meter) was recorded → a second run the same day is suppressed.
    again = await run_meter_nudges(send, now=_at(4))
    assert all(p.provider_name != "Газ (постачання)" for p in again)
