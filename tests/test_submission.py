from __future__ import annotations

from decimal import Decimal

from dvoretskyi.agent import tools
from dvoretskyi.agent.submission import ManualAssistChannel, SmsChannel, channel_for
from dvoretskyi.db.models import (
    Category,
    MeterReading,
    MeterStatus,
    PayChannel,
    Provider,
)
from tests.conftest import FakeVisionProvider


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


# --- confirm / "відправив" over the DB (default ManualAssist) ---------------


async def test_confirm_then_mark_submitted_flow(session, providers):
    # A flagged reading: baseline 1000, then 500 (backwards) → needs_confirm.
    await tools.submit_meter_reading(
        session, "Газ (постачання)", "/p.png", vision=FakeVisionProvider(Decimal("1000"))
    )
    flagged = await tools.submit_meter_reading(
        session, "Газ (постачання)", "/p.png", vision=FakeVisionProvider(Decimal("1500"))
    )
    # (1500 is a normal +500 jump under the abs cap → validated, not flagged)
    assert flagged["status"] == MeterStatus.validated.value

    # Force a genuine needs_confirm via a backwards reading.
    flagged = await tools.submit_meter_reading(
        session, "Газ (постачання)", "/p.png", vision=FakeVisionProvider(Decimal("900"))
    )
    assert flagged["status"] == MeterStatus.needs_confirm.value
    rid = flagged["reading_id"]

    # Confirm → ManualAssist keeps it validated (hands back instructions), not submitted.
    confirmed = await tools.confirm_meter_reading(session, rid)
    assert confirmed["status"] == MeterStatus.validated.value
    assert not confirmed["submitted"]

    # "відправив" → submitted.
    done = await tools.mark_meter_submitted(session, rid)
    assert done["status"] == MeterStatus.submitted.value
    row = await session.get(MeterReading, rid)
    assert row.status is MeterStatus.submitted and row.submitted_at is not None
