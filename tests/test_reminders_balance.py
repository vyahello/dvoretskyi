from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from dvoretskyi import clock
from dvoretskyi.agent.balance import Balance
from dvoretskyi.agent.tools import snooze_reminder
from dvoretskyi.db.models import (
    Category,
    NudgeKind,
    NudgeLog,
    PayChannel,
    Provider,
)
from dvoretskyi.reminders.engine import (
    compute_pending_balance_nudges,
    run_balance_nudges,
)


def _now() -> datetime:
    return datetime(2026, 6, 14, 11, 0, tzinfo=clock.KYIV)


def _fetch(balance, ok=True):
    async def fake():
        return Balance(balance, "2026-06-14", ok=ok)

    return fake


async def test_low_balance_fires(session, providers):
    # providers fixture has «Інтернет (Gigabit+)»; fee default 200.
    pending = await compute_pending_balance_nudges(
        session, _now(), fetch=_fetch(Decimal("120.00"))
    )
    assert len(pending) == 1
    p = pending[0]
    assert p.provider_name == "Інтернет (Gigabit+)"
    assert "120" in p.message() and "поповнити" in p.message().lower()


async def test_sufficient_balance_no_nudge(session, providers):
    pending = await compute_pending_balance_nudges(
        session, _now(), fetch=_fetch(Decimal("400.00"))
    )
    assert pending == []


async def test_fetch_failure_no_nudge(session, providers):
    pending = await compute_pending_balance_nudges(
        session, _now(), fetch=_fetch(None, ok=False)
    )
    assert pending == []


async def test_no_gigabit_provider_no_nudge(session):
    # Only a non-balance provider exists → nothing to check.
    session.add(
        Provider(
            name="Газ (постачання)",
            category=Category.gas,
            pay_channel=PayChannel.mono_communal,
            due_day=20,
        )
    )
    await session.commit()
    pending = await compute_pending_balance_nudges(
        session, _now(), fetch=_fetch(Decimal("1.00"))
    )
    assert pending == []


async def test_suppressed_when_snoozed(session, providers):
    gig = providers["Інтернет (Gigabit+)"]
    session.add(
        NudgeLog(
            provider_id=gig.id,
            cycle="2026-06",
            kind=NudgeKind.balance,
            nudged_at=_now(),
            snoozed_until=_now() + timedelta(days=3),
        )
    )
    await session.commit()
    pending = await compute_pending_balance_nudges(
        session, _now(), fetch=_fetch(Decimal("120.00"))
    )
    assert pending == []


async def test_snooze_reminder_silences_balance(session, providers):
    # The snooze tool snoozes the balance nudge for Gigabit+ too.
    await snooze_reminder(session, "Інтернет (Gigabit+)", "3")
    await session.commit()
    pending = await compute_pending_balance_nudges(
        session, _now(), fetch=_fetch(Decimal("120.00"))
    )
    assert pending == []


async def test_run_balance_nudges_sends_and_dedupes_same_day(session, providers):
    sent: list[tuple[int, str]] = []

    async def send(chat_id, text):
        sent.append((chat_id, text))

    pending = await run_balance_nudges(send, _now(), fetch=_fetch(Decimal("120.00")))
    assert pending and sent and "120" in sent[0][1]

    again = await run_balance_nudges(send, _now(), fetch=_fetch(Decimal("120.00")))
    assert again == []  # already nudged today
