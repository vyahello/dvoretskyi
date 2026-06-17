from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from dvoretskyi import clock
from dvoretskyi.bot import app as bot_app
from dvoretskyi.config import get_settings
from dvoretskyi.db.models import MeterReading, MeterStatus


def _at(day: int, *, month: int = 6, year: int = 2026) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=clock.KYIV)


def _first_cb(kb) -> str:
    return kb.inline_keyboard[0][0].callback_data


def _validated(rid: int = 7, value: str = "100.5", cons: str | None = "1.2") -> dict:
    return {
        "reading_id": rid,
        "status": MeterStatus.validated.value,
        "kind": "water",
        "value": value,
        "consumption": cons,
    }


# --- _gated_meter_reply: the date decides which action we offer --------------


def test_gated_reply_in_window_offers_approve():
    text, kb = bot_app._gated_meter_reply(_validated(), now=_at(29))
    assert "Холодна вода" in text and "100.5" in text  # names meter + value
    assert "Вікно подачі відкрите" in text
    assert _first_cb(kb) == "sf:7"  # one tap files it


def test_gated_reply_before_window_offers_early():
    text, kb = bot_app._gated_meter_reply(_validated(cons=None), now=_at(10))
    assert "Подам у вікні" in text and "тисни нижче" in text
    assert _first_cb(kb) == "se:7:1"  # «подай раніше», attempt 1


# --- _file_reading: enabled → submitted; disabled → manual fallback ----------


async def test_file_reading_disabled_falls_back_to_manual(
    engine, providers, session, monkeypatch
):
    monkeypatch.setattr(get_settings(), "infolv_submit_enabled", False)
    gas = providers["Газ (постачання)"]
    r = MeterReading(
        provider_id=gas.id,
        cycle="2026-06",
        value=Decimal("1877.78"),
        status=MeterStatus.validated,
        created_at=clock.now(),
    )
    session.add(r)
    await session.commit()

    text, kb = await bot_app._file_reading(r.id)
    assert "Подай на порталі infolviv" in text
    assert _first_cb(kb) == f"ms:{r.id}"  # falls back to the «Відправив ✓» tap
    await session.refresh(r)
    assert r.status is MeterStatus.validated  # not marked submitted by us


async def test_file_reading_surfaces_portal_rejection(
    engine, providers, session, monkeypatch
):
    from dvoretskyi.agent.infolviv import InfolvivSubmitError

    async def fake_submit(kind, value, **kw):
        raise InfolvivSubmitError("показник менший за поточний на порталі (106.4)")

    monkeypatch.setattr(bot_app, "submit_infolviv_reading", fake_submit)
    gas = providers["Газ (постачання)"]
    r = MeterReading(
        provider_id=gas.id,
        cycle="2026-06",
        value=Decimal("12.0"),
        status=MeterStatus.validated,
        created_at=clock.now(),
    )
    session.add(r)
    await session.commit()

    text, kb = await bot_app._file_reading(r.id)
    # The meter is named so the user knows which reading was refused.
    assert "не прийняв" in text and "Газ" in text and "менший за поточний" in text
    assert kb is None
    await session.refresh(r)
    assert r.status is MeterStatus.validated  # kept; a corrected re-photo replaces it


async def test_file_reading_enabled_marks_submitted(
    engine, providers, session, monkeypatch
):
    async def fake_submit(kind, value, **kw):
        assert kind == "water"
        return 222

    monkeypatch.setattr(bot_app, "submit_infolviv_reading", fake_submit)
    water = providers["Холодна вода"]
    r = MeterReading(
        provider_id=water.id,
        cycle="2026-06",
        value=Decimal("106.4"),
        status=MeterStatus.validated,
        created_at=clock.now(),
    )
    session.add(r)
    await session.commit()

    text, kb = await bot_app._file_reading(r.id)
    # Names the meter (which one was filed) alongside the value.
    assert "Подав на infolviv" in text and "106.4" in text
    assert "Холодна вода" in text
    assert kb is None
    await session.refresh(r)
    assert r.status is MeterStatus.submitted
    assert r.submitted_at is not None


# --- early insistence: resist twice, file on the 3rd «подай раніше» tap ------


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw) -> None:
        self.sent.append((text, reply_markup))


class _FakeCallback:
    """Duck-typed CallbackQuery; .message is NOT an aiogram Message, so _edit routes
    through bot.send_message (which we record)."""

    def __init__(self, data: str) -> None:
        self.data = data
        self.bot = _FakeBot()
        self.message = type("M", (), {"chat": type("C", (), {"id": 1})()})()
        self.answered = False

    async def answer(self, text: str | None = None, **kw) -> None:
        self.answered = True


async def test_early_insistence_resists_twice_then_files(
    engine, providers, session, monkeypatch
):
    monkeypatch.setattr(bot_app.clock, "now", lambda: _at(10))  # before the window
    monkeypatch.setattr(get_settings(), "infolv_submit_enabled", False)
    gas = providers["Газ (постачання)"]
    r = MeterReading(
        provider_id=gas.id,
        cycle="2026-06",
        value=Decimal("1877.78"),
        status=MeterStatus.validated,
        created_at=_at(10),
    )
    session.add(r)
    await session.commit()

    # tap 1 → pushback, button now carries attempt 2
    cb = _FakeCallback(f"se:{r.id}:1")
    await bot_app.on_meter_early(cb)
    text, kb = cb.bot.sent[-1]
    assert "зачека" in text.lower() or "рано" in text.lower()
    assert _first_cb(kb) == f"se:{r.id}:2"

    # tap 2 → still resists, button carries attempt 3
    cb = _FakeCallback(f"se:{r.id}:2")
    await bot_app.on_meter_early(cb)
    _, kb = cb.bot.sent[-1]
    assert _first_cb(kb) == f"se:{r.id}:3"

    # tap 3 → files (live POST disabled → manual fallback message)
    cb = _FakeCallback(f"se:{r.id}:3")
    await bot_app.on_meter_early(cb)
    text, _ = cb.bot.sent[-1]
    assert "Подай на порталі infolviv" in text


async def test_approve_in_window_files(engine, providers, session, monkeypatch):
    monkeypatch.setattr(get_settings(), "infolv_submit_enabled", False)
    water = providers["Холодна вода"]
    r = MeterReading(
        provider_id=water.id,
        cycle="2026-06",
        value=Decimal("106.4"),
        status=MeterStatus.validated,
        created_at=clock.now(),
    )
    session.add(r)
    await session.commit()

    cb = _FakeCallback(f"sf:{r.id}")
    await bot_app.on_meter_approve(cb)
    text, _ = cb.bot.sent[-1]
    assert "Подай на порталі infolviv" in text  # disabled → manual fallback
    assert cb.answered


# --- delete_meter_reading: remove a wrong reading from memory ----------------


async def test_delete_meter_reading_by_id_removes_row(engine, providers, session):
    from sqlalchemy import select

    from dvoretskyi.agent.tools import delete_meter_reading
    from dvoretskyi.db.models import MeterReading as MR

    gas = providers["Газ (постачання)"]
    r = MR(
        provider_id=gas.id,
        cycle="2026-06",
        value=Decimal("12.0"),
        status=MeterStatus.validated,
        created_at=clock.now(),
    )
    session.add(r)
    await session.commit()
    rid = r.id

    result = await delete_meter_reading(session, reading_id=rid)
    assert result["ok"] and "🗑" in result["message"] and "12.0" in result["message"]
    remaining = (await session.execute(select(MR))).scalars().all()
    assert remaining == []  # actually gone from memory


async def test_delete_meter_reading_refuses_submitted(engine, providers, session):
    import pytest

    from dvoretskyi.agent.tools import ToolError, delete_meter_reading
    from dvoretskyi.db.models import MeterReading as MR

    water = providers["Холодна вода"]
    r = MR(
        provider_id=water.id,
        cycle="2026-06",
        value=Decimal("106.4"),
        status=MeterStatus.submitted,
        created_at=clock.now(),
    )
    session.add(r)
    await session.commit()

    with pytest.raises(ToolError, match="подано на портал"):
        await delete_meter_reading(session, reading_id=r.id)


# --- delete-all needs confirmation (ask before wiping) -----------------------


async def test_conversational_delete_asks_before_wiping(engine, providers, session):
    from dvoretskyi.agent.tools import delete_meter_reading
    from dvoretskyi.db.models import MeterReading as MR

    for prov, val in (
        (providers["Газ (постачання)"], "1877.78"),
        (providers["Холодна вода"], "103.999"),
    ):
        session.add(
            MR(
                provider_id=prov.id,
                cycle="2026-06",
                value=Decimal(val),
                status=MeterStatus.validated,
                created_at=clock.now(),
            )
        )
    await session.commit()

    # No reading_id → must NOT delete; returns a confirmation for ALL readings.
    result = await delete_meter_reading(session)
    assert result.get("confirm_delete") is True
    assert result["confirm_scope"] == "all" and result["count"] == 2
    from sqlalchemy import select

    assert len((await session.execute(select(MR))).scalars().all()) == 2  # still there


async def test_fresh_reading_supersedes_older_draft(engine, providers, session):
    # Two photos of the SAME meter → only the freshest draft survives (no pile-up).
    from sqlalchemy import select

    from dvoretskyi.agent.tools import submit_meter_reading
    from dvoretskyi.agent.vision import MeterRead
    from dvoretskyi.db.models import MeterReading as MR

    water = providers["Холодна вода"]
    for val in ("100.500", "100.700"):
        await submit_meter_reading(
            session,
            water.name,
            "/tmp/fake.png",
            read=MeterRead(value=Decimal(val), raw="", note="", kind="water"),
            auto_submit=False,
        )
    rows = (await session.execute(select(MR))).scalars().all()
    assert len(rows) == 1  # the older draft was superseded
    assert str(rows[0].value) == "100.700"  # only the freshest is kept


async def test_supersede_keeps_submitted_history(engine, providers, session):
    # A filed (submitted) reading is the permanent record — a new draft must NOT wipe it.
    from sqlalchemy import select

    from dvoretskyi.agent.tools import submit_meter_reading
    from dvoretskyi.agent.vision import MeterRead
    from dvoretskyi.db.models import MeterReading as MR

    water = providers["Холодна вода"]
    session.add(
        MR(
            provider_id=water.id,
            cycle="2026-05",
            value=Decimal("99.000"),
            status=MeterStatus.submitted,
            created_at=clock.now(),
        )
    )
    await session.commit()

    await submit_meter_reading(
        session,
        water.name,
        "/tmp/fake.png",
        read=MeterRead(value=Decimal("100.500"), raw="", note="", kind="water"),
        auto_submit=False,
    )
    rows = (await session.execute(select(MR))).scalars().all()
    statuses = {r.status for r in rows}
    assert MeterStatus.submitted in statuses  # filed reading preserved
    assert len(rows) == 2


async def test_delete_scoped_to_provider_and_cycle(engine, providers, session):
    # «видали показник газу за травень» → only that meter + month is targeted.
    from dvoretskyi.agent.tools import delete_meter_reading, execute_meter_delete
    from dvoretskyi.db.models import MeterReading as MR

    gas = providers["Газ (постачання)"]
    water = providers["Холодна вода"]
    for prov, cyc, val in (
        (gas, "2026-05", "1800.00"),
        (gas, "2026-06", "1877.78"),
        (water, "2026-05", "100.500"),
    ):
        session.add(
            MR(
                provider_id=prov.id,
                cycle=cyc,
                value=Decimal(val),
                status=MeterStatus.validated,
                created_at=clock.now(),
            )
        )
    await session.commit()

    res = await delete_meter_reading(session, "Газ (постачання)", cycle="2026-05")
    assert res["confirm_delete"] and res["count"] == 1  # only the May gas reading
    assert "травень 2026" in res["message"] and "Газ (постачання)" in res["message"]

    from sqlalchemy import select

    out = await execute_meter_delete(session, res["confirm_scope"])
    assert out["deleted"] == 1
    survivors = {
        (r.provider_id, r.cycle)
        for r in (await session.execute(select(MR))).scalars().all()
    }
    assert (gas.id, "2026-05") not in survivors  # gone
    assert (gas.id, "2026-06") in survivors and (water.id, "2026-05") in survivors


async def test_execute_meter_delete_all_wipes(engine, providers, session):
    from sqlalchemy import select

    from dvoretskyi.agent.tools import execute_meter_delete
    from dvoretskyi.db.models import MeterReading as MR

    for prov, val in (
        (providers["Газ (постачання)"], "1877.78"),
        (providers["Холодна вода"], "103.999"),
    ):
        session.add(
            MR(
                provider_id=prov.id,
                cycle="2026-06",
                value=Decimal(val),
                status=MeterStatus.validated,
                created_at=clock.now(),
            )
        )
    # one already submitted → must survive a wipe
    session.add(
        MR(
            provider_id=providers["Холодна вода"].id,
            cycle="2026-05",
            value=Decimal("100.0"),
            status=MeterStatus.submitted,
            created_at=clock.now(),
        )
    )
    await session.commit()

    result = await execute_meter_delete(session, "all")
    assert result["deleted"] == 2
    remaining = (await session.execute(select(MR))).scalars().all()
    assert len(remaining) == 1 and remaining[0].status is MeterStatus.submitted
