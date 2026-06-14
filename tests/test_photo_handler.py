from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from aiogram.types import InlineKeyboardMarkup

from komunalka import clock
from komunalka.bot import app as bot_app
from komunalka.db.models import MeterReading, MeterStatus
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


def _freeze(monkeypatch, day: int) -> None:
    fixed = datetime(2026, 6, day, 12, 0, tzinfo=clock.KYIV)
    monkeypatch.setattr(clock, "now", lambda: fixed)


def _patch_io(monkeypatch, value) -> FakeVisionProvider:
    vis = FakeVisionProvider(value)

    async def fake_download(message):
        return "/tmp/komunalka_fake_meter.png"

    monkeypatch.setattr(bot_app, "_download_photo", fake_download)
    monkeypatch.setattr(bot_app, "get_vision_provider", lambda: vis)
    return vis


async def test_single_meter_in_window_auto_routes(engine, providers, monkeypatch):
    _freeze(monkeypatch, 4)  # gas window (2..5); water (22..25) closed → one candidate
    vis = _patch_io(monkeypatch, Decimal("1000"))

    msg = FakeMessage()
    await bot_app.on_photo(msg)

    assert vis.calls, "OCR should have run on the auto-routed provider"
    text, kb = msg.answers[-1]
    assert "Газ (постачання)" in text  # ManualAssist confirmation names the provider
    # validated reading → offers the «Відправив ✓» button, not a routing question
    assert isinstance(kb, InlineKeyboardMarkup)


async def test_no_meter_in_window_asks_which(engine, providers, monkeypatch):
    _freeze(monkeypatch, 12)  # neither gas nor water window is open
    vis = _patch_io(monkeypatch, Decimal("1000"))

    msg = FakeMessage()
    await bot_app.on_photo(msg)

    assert not vis.calls, "must not OCR until the user says which meter"
    text, kb = msg.answers[-1]
    assert "Який це лічильник" in text
    assert isinstance(kb, InlineKeyboardMarkup)
    # one button per meter provider (gas + water)
    assert sum(len(row) for row in kb.inline_keyboard) == 2


async def test_caption_routes_even_outside_window(engine, providers, monkeypatch):
    _freeze(monkeypatch, 12)  # outside any window, but caption disambiguates
    vis = _patch_io(monkeypatch, Decimal("1000"))

    msg = FakeMessage(caption="показники газу")
    await bot_app.on_photo(msg)

    assert vis.calls
    text, _ = msg.answers[-1]
    assert "Газ (постачання)" in text


async def test_ambiguous_capture_persists_ocr_pending_row(engine, providers, monkeypatch):
    _freeze(monkeypatch, 12)
    _patch_io(monkeypatch, Decimal("1000"))

    msg = FakeMessage()
    await bot_app.on_photo(msg)

    async with bot_app.session_scope() as session:
        from sqlalchemy import select

        rows = (await session.execute(select(MeterReading))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status is MeterStatus.ocr_pending
    assert rows[0].photo_ref == "/tmp/komunalka_fake_meter.png"
