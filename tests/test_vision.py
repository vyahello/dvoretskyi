from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from dvoretskyi.agent import tools
from dvoretskyi.agent.vision import _extract_json_object, _parse_meter_read
from dvoretskyi.db.models import MeterReading, MeterStatus
from tests.conftest import FakeVisionProvider


async def _last_reading(session, provider_id: int) -> MeterReading:
    rows = (
        (
            await session.execute(
                select(MeterReading)
                .where(MeterReading.provider_id == provider_id)
                .order_by(MeterReading.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return rows[0]


# --- pipeline over a fake VisionProvider (no real claude) ------------------


async def test_pipeline_valid_value_validates_and_hands_back(session, providers):
    res = await tools.submit_meter_reading(
        session, "Газ (постачання)", "/p.png", vision=FakeVisionProvider(Decimal("1000"))
    )
    assert res["ok"]
    assert res["status"] == MeterStatus.validated.value
    assert not res["submitted"]  # ManualAssist hands it back, never auto-submits
    assert res["instructions"]


async def test_pipeline_ocr_failure_asks_to_retype(session, providers):
    res = await tools.submit_meter_reading(
        session, "Газ (постачання)", "/p.png", vision=FakeVisionProvider(None)
    )
    assert not res["ok"]
    assert res["status"] == MeterStatus.failed.value
    assert "вручну" in res["message"] or "перефотографуй" in res["message"].lower()


async def test_water_keeps_three_decimals(session, providers):
    res = await tools.submit_meter_reading(
        session, "Холодна вода", "/p.png", vision=FakeVisionProvider(Decimal("103.999"))
    )
    assert res["value"] == "103.999"  # not 103, not 104
    row = await _last_reading(session, providers["Холодна вода"].id)
    assert row.value == Decimal("103.999")


async def test_gas_keeps_two_decimals(session, providers):
    res = await tools.submit_meter_reading(
        session,
        "Газ (постачання)",
        "/p.png",
        vision=FakeVisionProvider(Decimal("4827.05")),
    )
    assert res["value"] == "4827.05"
    row = await _last_reading(session, providers["Газ (постачання)"].id)
    assert row.value == Decimal("4827.05")


async def test_value_rounded_half_up_to_provider_precision(session, providers):
    # Gas = 2 decimals: 4827.058 → 4827.06 (ROUND_HALF_UP).
    res = await tools.submit_meter_reading(
        session,
        "Газ (постачання)",
        "/p.png",
        vision=FakeVisionProvider(Decimal("4827.058")),
    )
    assert res["value"] == "4827.06"


async def test_delta_validation_runs_on_decimals(session, providers):
    # Two close water readings → small positive 3rd-decimal consumption → validated.
    await tools.submit_meter_reading(
        session, "Холодна вода", "/p.png", vision=FakeVisionProvider(Decimal("103.999"))
    )
    res = await tools.submit_meter_reading(
        session, "Холодна вода", "/p.png", vision=FakeVisionProvider(Decimal("104.250"))
    )
    assert res["status"] == MeterStatus.validated.value
    assert res["consumption"] == "0.251"


async def test_pipeline_flagged_reading_is_needs_confirm_not_submitted(
    session, providers
):
    # baseline, then a backwards reading → needs_confirm (never submitted)
    await tools.submit_meter_reading(
        session, "Газ (постачання)", "/p.png", vision=FakeVisionProvider(Decimal("1000"))
    )
    res = await tools.submit_meter_reading(
        session, "Газ (постачання)", "/p.png", vision=FakeVisionProvider(Decimal("500"))
    )
    assert not res["ok"]
    assert res["status"] == MeterStatus.needs_confirm.value


# --- parser robustness (chatty / fenced model output) ----------------------


def test_parser_extracts_json_from_prose_and_fence():
    chatty = (
        "Reading the drums: 0 4 8 2 7.\n\n"
        '```json\n{"value": 4827, "raw": "04827", "note": "ok"}\n```'
    )
    r = _parse_meter_read(chatty)
    assert r is not None and r.value == Decimal("4827")


def test_parser_null_value_and_garbage():
    assert _parse_meter_read('{"value": null, "raw": "", "note": "x"}').value is None
    assert _parse_meter_read("totally not json") is None
    assert _extract_json_object("no object here") is None


def test_parser_reads_kind_and_comment():
    # Dark meter → water; light → gas; not a meter → other + a joke.
    water = _parse_meter_read('{"kind": "WATER", "value": "55.123", "raw": "55123"}')
    assert water is not None and water.kind == "water"  # normalized to lowercase
    other = _parse_meter_read(
        '{"kind": "other", "value": null, "comment": "Гарний кіт."}'
    )
    assert other is not None and other.kind == "other"
    assert other.value is None and other.comment == "Гарний кіт."
