from __future__ import annotations

import os
from decimal import Decimal

from dvoretskyi import clock
from dvoretskyi.bot import app as bot_app
from dvoretskyi.bot.app import (
    _format_stats,
    _format_unpaid,
    cmd_help,
    cmd_start,
    cmd_stats,
    cmd_unpaid,
    on_text,
)
from dvoretskyi.db.models import Payment, PaymentSource


class FakeMessage:
    """Duck-typed aiogram Message that records what the handler sent."""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[str] = []
        self.photos: list[tuple[object, str | None]] = []

    async def answer(self, text: str, **kw) -> None:
        self.answers.append(text)

    async def answer_photo(self, photo, caption: str | None = None, **kw) -> None:
        self.photos.append((photo, caption))


def _payment(provider_id, amount, tx):
    return Payment(
        provider_id=provider_id,
        amount_uah=Decimal(amount),
        paid_at=clock.now(),
        source=PaymentSource.mono_webhook,
        raw_description="",
        mono_tx_id=tx,
    )


# --- /start, /help (static) ------------------------------------------------


async def test_cmd_start(engine):
    msg = FakeMessage()
    await cmd_start(msg)
    assert len(msg.answers) == 1
    text = msg.answers[0]
    assert "/unpaid" in text and "/help" in text


async def test_cmd_help(engine):
    msg = FakeMessage()
    await cmd_help(msg)
    text = msg.answers[0]
    for cmd in ("/unpaid", "/stats", "/help"):
        assert cmd in text
    assert "заплатити" in text  # free-text hint


# --- /unpaid ---------------------------------------------------------------


async def test_cmd_unpaid_lists_open(engine, providers):
    msg = FakeMessage()
    await cmd_unpaid(msg)
    text = msg.answers[0]
    assert text.startswith("Відкрите цього місяця:")
    assert "Газ (постачання)" in text
    # water has an expected_amount → shown as a hint
    assert "≈180.00 ₴" in text


async def test_cmd_unpaid_all_clear(engine, providers, session):
    for tx, prov in enumerate(providers.values()):
        session.add(_payment(prov.id, "100.00", f"clear-{tx}"))
    await session.commit()

    msg = FakeMessage()
    await cmd_unpaid(msg)
    assert msg.answers[0].startswith("✅ Усе чисто")


def test_format_unpaid_all_clear_unit():
    assert _format_unpaid({"all_clear": True, "open": []}).startswith("✅ Усе чисто")


# --- /stats ----------------------------------------------------------------


async def test_cmd_stats_with_data_sends_photo(engine, providers, session):
    gas = providers["Газ (постачання)"]
    session.add(_payment(gas.id, "480.00", "st1"))
    await session.commit()

    msg = FakeMessage()
    await cmd_stats(msg)
    assert msg.photos and not msg.answers  # chart sent as photo, no text answer
    _photo, caption = msg.photos[0]
    assert "480.00" in caption and clock.current_cycle() in caption

    # The handler created a real PNG; clean it up.
    path = msg.photos[0][0].path
    if os.path.exists(path):
        os.unlink(path)


async def test_cmd_stats_empty_sends_text(engine, providers):
    msg = FakeMessage()
    await cmd_stats(msg)
    assert msg.answers and not msg.photos
    assert "порожньо" in msg.answers[0]


def test_format_stats_empty_unit():
    out = _format_stats({"period": "2026-06", "total": "0", "items": []})
    assert "порожньо" in out


# --- free-text error path: never leave the user with silence ----------------


async def test_on_text_replies_on_error(engine, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("kaboom")

    # Force a failure anywhere in the agent path (DB/context/LLM all funnel here).
    monkeypatch.setattr(bot_app.agent_dispatcher, "handle_message", boom)

    msg = FakeMessage(text="що треба заплатити?")
    await on_text(msg)  # must not raise
    assert msg.answers and "заклинило" in msg.answers[0]
