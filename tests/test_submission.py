from __future__ import annotations

from decimal import Decimal

from komunalka.agent.submission import ManualAssistChannel, SmsChannel, channel_for
from komunalka.db.models import (
    Category,
    MeterReading,
    MeterStatus,
    PayChannel,
    Provider,
)


def _gas() -> tuple[Provider, MeterReading]:
    prov = Provider(
        name="Газ (постачання)",
        category=Category.gas,
        pay_channel=PayChannel.mono_communal,
        account_number="123456",
    )
    reading = MeterReading(
        cycle="2026-06", value=Decimal("4827"), status=MeterStatus.validated
    )
    reading.provider = prov
    return prov, reading


async def test_manual_assist_returns_instructions_and_keeps_validated():
    prov, reading = _gas()
    res = await ManualAssistChannel().submit(prov, reading)
    assert res.status is MeterStatus.validated
    assert not res.submitted
    assert res.instructions and "4647" in res.instructions
    assert res.deep_link and res.deep_link.startswith("sms:4647")
    assert "4827" in res.message


async def test_manual_assist_is_the_default_channel():
    prov, _ = _gas()
    assert isinstance(channel_for(prov), ManualAssistChannel)


async def test_sms_channel_dry_run_formats_body_without_posting():
    prov, reading = _gas()
    res = await SmsChannel(dry_run=True).submit(prov, reading)
    assert not res.submitted
    assert res.status is MeterStatus.validated
    # account number + reading, no decimals
    assert "123456 4827" in res.instructions
    assert res.message.startswith("[dry-run]")
