from __future__ import annotations

from decimal import Decimal

from komunalka.agent import tools
from komunalka.agent.vision import _extract_json_object, _parse_meter_read
from komunalka.db.models import MeterStatus
from tests.conftest import FakeVisionProvider

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
