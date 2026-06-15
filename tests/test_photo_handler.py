from __future__ import annotations

from decimal import Decimal

from aiogram.types import InlineKeyboardMarkup

from dvoretskyi.bot import app as bot_app
from dvoretskyi.db.models import MeterReading, MeterStatus
from tests.conftest import FakeVisionProvider


class FakeMessage:
    """Duck-typed Message: has a photo, records answers (text + keyboard)."""

    def __init__(self, caption: str | None = None) -> None:
        self.photo = [object()]  # truthy; download is monkeypatched
        self.caption = caption
        self.bot = object()
        self.answers: list[tuple[str, object]] = []

    async def answer(self, text: str, reply_markup=None, **kw) -> None:
        self.answers.append((text, reply_markup))


def _patch_io(
    monkeypatch, value, *, kind: str = "", comment: str = ""
) -> FakeVisionProvider:
    vis = FakeVisionProvider(value, kind=kind, comment=comment)

    async def fake_download(message):
        return "/tmp/dvoretskyi_fake_meter.png"

    monkeypatch.setattr(bot_app, "_download_photo", fake_download)
    monkeypatch.setattr(bot_app, "get_vision_provider", lambda: vis)
    return vis


async def _stored_provider_id(session):
    from sqlalchemy import select

    reading = (await session.execute(select(MeterReading))).scalars().one()
    return reading.provider_id, reading.status


async def test_light_meter_auto_routes_to_gas(engine, providers, monkeypatch):
    # Vision says it's a light meter → gas, no window/no question.
    vis = _patch_io(monkeypatch, Decimal("1000"), kind="gas")

    msg = FakeMessage()
    await bot_app.on_photo(msg)

    assert vis.calls, "OCR should have run once"
    text, kb = msg.answers[-1]
    assert "Записав показник 1000" in text  # stored, not yet submitted
    assert isinstance(kb, InlineKeyboardMarkup)  # approve / «подай раніше» button
    async with bot_app.session_scope() as session:
        pid, status = await _stored_provider_id(session)
    assert pid == providers["Газ (постачання)"].id
    assert status is MeterStatus.validated  # validated, awaiting the date gate


async def test_dark_meter_auto_routes_to_water(engine, providers, monkeypatch):
    # Vision says it's a dark meter → water.
    vis = _patch_io(monkeypatch, Decimal("55"), kind="water")

    msg = FakeMessage()
    await bot_app.on_photo(msg)

    assert vis.calls
    async with bot_app.session_scope() as session:
        pid, _ = await _stored_provider_id(session)
    assert pid == providers["Холодна вода"].id


async def test_non_meter_photo_gets_a_joke(engine, providers, monkeypatch):
    # Not a meter → witty remark, nothing stored, no routing question.
    vis = _patch_io(
        monkeypatch,
        None,
        kind="other",
        comment="Симпатичний кіт, але рахунків не платить.",
    )

    msg = FakeMessage()
    await bot_app.on_photo(msg)

    assert len(vis.calls) == 1
    text, kb = msg.answers[-1]
    assert text == "Симпатичний кіт, але рахунків не платить."
    assert kb is None

    async with bot_app.session_scope() as session:
        from sqlalchemy import select

        rows = (await session.execute(select(MeterReading))).scalars().all()
    assert rows == []  # a non-meter photo never touches the meter table


async def test_caption_overrides_detected_kind(engine, providers, monkeypatch):
    # Caption names the meter explicitly → wins over whatever vision guessed.
    vis = _patch_io(monkeypatch, Decimal("1000"), kind="water")

    msg = FakeMessage(caption="показники газу")
    await bot_app.on_photo(msg)

    assert vis.calls
    async with bot_app.session_scope() as session:
        pid, _ = await _stored_provider_id(session)
    assert pid == providers["Газ (постачання)"].id  # caption won over vision's "water"


async def test_unknown_kind_asks_which(engine, providers, monkeypatch):
    # Vision couldn't classify (kind="") and there's no caption → ask which meter.
    _patch_io(monkeypatch, Decimal("1000"), kind="")

    msg = FakeMessage()
    await bot_app.on_photo(msg)

    text, kb = msg.answers[-1]
    assert "Який це лічильник" in text
    assert isinstance(kb, InlineKeyboardMarkup)
    # one button per meter provider (gas + water)
    assert sum(len(row) for row in kb.inline_keyboard) == 2

    async with bot_app.session_scope() as session:
        from sqlalchemy import select

        rows = (await session.execute(select(MeterReading))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status is MeterStatus.ocr_pending
    assert rows[0].photo_ref == "/tmp/dvoretskyi_fake_meter.png"
