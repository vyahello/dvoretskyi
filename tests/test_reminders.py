from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from komunalka import clock
from komunalka.db.models import NudgeKind, NudgeLog, Payment, PaymentSource
from komunalka.reminders import engine

# Гас due_day=15. Pick a "now" inside the window (12..15), near deadline at 14/15.
NOW = datetime(2026, 6, 14, 10, 0, tzinfo=clock.KYIV)


def _cycle(now=NOW) -> str:
    return clock.cycle_of(now)


async def test_nudge_fires_for_overdue_unpaid(session, providers):
    pending = await engine.compute_pending_nudges(session, now=NOW)
    names = {p.provider_name for p in pending}
    assert "Газ (постачання)" in names
    gas = next(p for p in pending if p.provider_name == "Газ (постачання)")
    assert gas.near_deadline is True  # today=14, due-1=14


async def test_outside_window_not_nudged(session, providers):
    early = datetime(2026, 6, 5, 10, 0, tzinfo=clock.KYIV)  # before due_day-3 for all
    pending = await engine.compute_pending_nudges(session, now=early)
    assert all(p.provider_name != "Газ (постачання)" for p in pending)


async def test_paid_suppresses_nudge(session, providers):
    gas = providers["Газ (постачання)"]
    session.add(
        Payment(
            provider_id=gas.id, amount_uah=Decimal("480.00"), paid_at=NOW,
            source=PaymentSource.mono_webhook, raw_description="", mono_tx_id="p1",
        )
    )
    await session.commit()
    pending = await engine.compute_pending_nudges(session, now=NOW)
    assert all(p.provider_name != "Газ (постачання)" for p in pending)


async def test_snoozed_suppresses_nudge(session, providers):
    gas = providers["Газ (постачання)"]
    session.add(
        NudgeLog(
            provider_id=gas.id, cycle=_cycle(), kind=NudgeKind.payment,
            nudged_at=NOW - timedelta(days=2), snoozed_until=NOW + timedelta(days=2),
        )
    )
    await session.commit()
    pending = await engine.compute_pending_nudges(session, now=NOW)
    assert all(p.provider_name != "Газ (постачання)" for p in pending)


async def test_already_nudged_today_suppressed(session, providers):
    gas = providers["Газ (постачання)"]
    session.add(
        NudgeLog(
            provider_id=gas.id, cycle=_cycle(), kind=NudgeKind.payment,
            nudged_at=NOW - timedelta(hours=2), snoozed_until=None,
        )
    )
    await session.commit()
    pending = await engine.compute_pending_nudges(session, now=NOW)
    assert all(p.provider_name != "Газ (постачання)" for p in pending)


async def test_run_payment_nudges_sends_once_per_day(session, providers):
    sent: list[tuple[int, str]] = []

    async def fake_send(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    fired = await engine.run_payment_nudges(fake_send, now=NOW)
    assert any(p.provider_name == "Газ (постачання)" for p in fired)
    assert len(sent) == len(fired) and sent  # something was sent

    # Second run the same day → already-nudged suppression kicks in.
    sent.clear()
    again = await engine.run_payment_nudges(fake_send, now=NOW)
    assert again == []
    assert sent == []
