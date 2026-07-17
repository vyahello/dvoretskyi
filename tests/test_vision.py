from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from dvoretskyi import clock
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


async def _seed_prior(session, provider, value: str, cycle: str = "2026-05") -> None:
    """A filed reading from a PRIOR month — the real validation baseline (consumption is
    always measured against an earlier cycle, never a same-month re-shoot)."""
    session.add(
        MeterReading(
            provider_id=provider.id,
            cycle=cycle,
            value=Decimal(value),
            status=MeterStatus.submitted,
            created_at=clock.now(),
        )
    )
    await session.commit()


async def test_delta_validation_runs_on_decimals(session, providers):
    # Prior-month baseline, then this month's reading → small positive consumption.
    await _seed_prior(session, providers["Холодна вода"], "103.999")
    res = await tools.submit_meter_reading(
        session, "Холодна вода", "/p.png", vision=FakeVisionProvider(Decimal("104.250"))
    )
    assert res["status"] == MeterStatus.validated.value
    assert res["consumption"] == "0.251"


async def test_pipeline_flagged_reading_is_needs_confirm_not_submitted(
    session, providers
):
    # prior-month baseline, then a backwards reading → needs_confirm (never submitted)
    await _seed_prior(session, providers["Газ (постачання)"], "1000")
    res = await tools.submit_meter_reading(
        session, "Газ (постачання)", "/p.png", vision=FakeVisionProvider(Decimal("500"))
    )
    assert not res["ok"]
    assert res["status"] == MeterStatus.needs_confirm.value


async def test_low_confidence_ocr_is_flagged_even_when_delta_plausible(
    session, providers
):
    # Independent reads disagreed (108.679 vs 148.679) — a believable +40, so the spike
    # check passes, but the disagreement must still force needs_confirm.
    from dvoretskyi.agent.vision import MeterRead

    await _seed_prior(session, providers["Холодна вода"], "108.000")
    read = MeterRead(
        value=Decimal("148.679"),
        raw="148679",
        note="",
        kind="water",
        confident=False,
        alt_value=Decimal("108.679"),
    )
    res = await tools.submit_meter_reading(session, "Холодна вода", "/p.png", read=read)
    assert not res["ok"]
    assert res["status"] == MeterStatus.needs_confirm.value
    assert "108.679" in res["message"]  # surfaces the differing read


async def test_hint_guided_reread_corrects_an_ambiguous_wheel(
    session, providers, monkeypatch
):
    """With a portal baseline of ~108, a blind read of 148.679 is re-read WITH the
    previous value as an anchor → the model resolves the ambiguous wheel back to 108.679,
    and that context-aware value is what gets stored."""
    from dvoretskyi.agent import infolviv as infolviv_mod
    from dvoretskyi.agent.infolviv import InfolvivReading
    from dvoretskyi.agent.vision import MeterRead, VisionProvider

    class _HintAwareVision(VisionProvider):
        """Misreads 148.679 blind, but reads 108.679 when given the anchor (like the real
        model resolving the rounded 0 once it knows the meter stood near 108)."""

        async def read_meter(self, image_path, hints=None):
            v = Decimal("108.679") if hints else Decimal("148.679")
            return MeterRead(value=v, raw=str(v), note="", kind="water")

    filed = InfolvivReading(
        kind="water",
        account_code="ACC-WATER-1",
        counter_number="111",
        provider="Львівводоканал",
        service="Вода",
        period="2026-05",
        value=Decimal("108.000"),
        difference=Decimal("1.0"),
        window_start_day=28,
        window_end_day=30,
        window_open=True,
        counter_id=111,
    )

    async def fake_reading_for_kind(kind, **kw):
        return filed if kind == "water" else None

    monkeypatch.setattr(infolviv_mod, "reading_for_kind", fake_reading_for_kind)

    res = await tools.submit_meter_reading(
        session, "Холодна вода", "/p.png", vision=_HintAwareVision()
    )
    assert res["value"] == "108.679"  # the anchored re-read, not the blind 148.679
    assert res["status"] == MeterStatus.validated.value


def test_reconcile_agreeing_reads_are_confident():
    from dvoretskyi.agent.vision import MeterRead, _reconcile

    a = MeterRead(value=Decimal("108.679"), raw="108679", note="", kind="water")
    b = MeterRead(value=Decimal("108.679"), raw="108679", note="", kind="water")
    out = _reconcile([a, b])
    assert out.confident and out.value == Decimal("108.679")


def test_reconcile_disagreeing_reads_flag_and_record_alt():
    from dvoretskyi.agent.vision import MeterRead, _reconcile

    a = MeterRead(value=Decimal("148.679"), raw="148679", note="", kind="water")
    b = MeterRead(value=Decimal("108.679"), raw="108679", note="", kind="water")
    out = _reconcile([a, b])
    assert not out.confident
    assert out.value == Decimal("148.679") and out.alt_value == Decimal("108.679")


def test_reconcile_ignores_a_failed_read_and_keeps_the_real_one():
    """A null/«other» read must not outvote a read that produced a number.

    Which of the parallel CLI calls misfires is random. Anchoring the verdict on
    reads[0] meant that when the flaky one landed first, a genuine meter photo was
    answered with «на фото не лічильник» + a joke and nothing was stored.
    """
    from dvoretskyi.agent.vision import MeterRead, _reconcile

    dud = MeterRead(value=None, raw="", note="", kind="other", comment="жарт")
    good = MeterRead(value=Decimal("108.679"), raw="108679", note="", kind="water")
    out = _reconcile([dud, good])
    assert out.value == Decimal("108.679")
    assert out.kind == "water"  # the verdict's kind comes from the read that saw a number
    assert not out.comment  # …and the dud's joke never rides along


def test_reconcile_majority_wins_over_a_single_outlier():
    """Two reads agreeing beat one that doesn't, whichever order they arrive in."""
    from dvoretskyi.agent.vision import MeterRead, _reconcile

    odd = MeterRead(value=Decimal("148.679"), raw="148679", note="", kind="water")
    a = MeterRead(value=Decimal("108.679"), raw="108679", note="", kind="water")
    b = MeterRead(value=Decimal("108.679"), raw="108679", note="", kind="water")
    out = _reconcile([odd, a, b])
    assert out.value == Decimal("108.679")  # 2:1 majority, not "whoever was first"
    assert out.confident and out.alt_value == Decimal("148.679")


def test_reconcile_all_failed_reads_stay_a_failure():
    """No read produced a number → a genuine OCR failure; never invent one."""
    from dvoretskyi.agent.vision import MeterRead, _reconcile

    dud = MeterRead(value=None, raw="", note="", kind="other", comment="жарт")
    out = _reconcile([dud, MeterRead(value=None, raw="", note="", kind="other")])
    assert out.value is None and out.kind == "other"


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


def test_parser_takes_the_LAST_object_when_prose_holds_several():
    """The answer comes last. A model that shows an example first must not have the
    example win — «Наприклад: {…}. Відповідь: {…}» has to resolve to the ANSWER.

    Scanning the braces forward instead of in reverse silently inverts this: the meter
    photo then reads as `kind=other` and the bot answers a real reading with a joke.
    Nothing else in the suite pins the ordering, so it lives here.
    """
    prose = (
        'Наприклад: {"kind": "other", "value": null}. '
        'Ось відповідь: {"kind": "gas", "value": "1888.14"}'
    )
    assert _extract_json_object(prose) == {"kind": "gas", "value": "1888.14"}
    read = _parse_meter_read(prose)
    assert read is not None and read.value == Decimal("1888.14") and read.kind == "gas"


def test_decision_parser_survives_a_line_of_preamble():
    """The decision turn gets the same forgiving extraction as the vision turn: one line
    of prose used to fail the parse outright, costing a second full 60s `claude -p` call
    and then a «мій мисленнєвий апарат зламався» apology — for good JSON with a sentence
    in front of it."""
    from dvoretskyi.agent.provider import parse_decision

    d = parse_decision(
        'Ось відповідь:\n{"tool": "get_stats", "args": {"period": "2026-05"}, '
        '"message": "Зараз"}'
    )
    assert d is not None and d.tool == "get_stats" and d.args == {"period": "2026-05"}


def test_decision_parser_guards_a_non_string_tool():
    """`{"tool": ["get_stats"]}` reached `TOOLS.get(decision.tool)` and raised
    «unhashable type: 'list'» past the arg-error handler."""
    from dvoretskyi.agent.provider import parse_decision

    d = parse_decision('{"tool": ["get_stats"], "args": {}, "message": "х"}')
    assert d is not None and d.tool is None  # treated as "no tool", never a crash


def test_parser_reads_kind_and_comment():
    # Dark meter → water; light → gas; not a meter → other + a joke.
    water = _parse_meter_read('{"kind": "WATER", "value": "55.123", "raw": "55123"}')
    assert water is not None and water.kind == "water"  # normalized to lowercase
    other = _parse_meter_read(
        '{"kind": "other", "value": null, "comment": "Гарний кіт."}'
    )
    assert other is not None and other.kind == "other"
    assert other.value is None and other.comment == "Гарний кіт."
