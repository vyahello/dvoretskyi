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
    assert "/help" in text and "дворецький" in text.lower()


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


# --- main-menu reply keyboard ----------------------------------------------


def test_main_keyboard_has_menu_buttons():
    from dvoretskyi.bot import keyboards

    labels = [b.text for row in keyboards.main_keyboard().keyboard for b in row]
    for lbl in (
        keyboards.MENU_UNPAID,
        keyboards.MENU_STATS,
        keyboards.MENU_BALANCE,
        keyboards.MENU_METERS,
        keyboards.MENU_HELLO,
        keyboards.MENU_HELP,
    ):
        assert lbl in labels


async def test_menu_button_meters_shows_infolviv_when_available(engine, monkeypatch):
    from dvoretskyi.agent.infolviv import InfolvivReading

    async def fake_fetch():
        return [
            InfolvivReading(
                kind="water",
                counter_number="10000001",  # fake — never a real account
                provider="ВОДОКАНАЛ (тест)",
                service="Централізоване водопостачання (ХВ)",
                period="2026-05",
                value=Decimal("100.500"),
                difference=Decimal("0.0"),
                window_start_day=1,
                window_end_day=10,
                window_open=True,
            )
        ]

    monkeypatch.setattr(bot_app, "fetch_infolviv_readings", fake_fetch)
    msg = FakeMessage()
    await bot_app.menu_meters(msg)
    out = msg.answers[0]
    assert "infolviv" in out
    assert "Холодна вода" in out and "100.500" in out
    assert "травень 2026" in out
    assert "1–10 число" in out


async def test_menu_button_meters_falls_back_to_journal(engine, providers, monkeypatch):
    # Portal unreachable / not configured → show the local photo-journal instead.
    async def empty_fetch():
        return []

    monkeypatch.setattr(bot_app, "fetch_infolviv_readings", empty_fetch)
    msg = FakeMessage()
    await bot_app.menu_meters(msg)  # nothing stored yet → empty-journal hint
    assert "журнал чистий" in msg.answers[0]
    assert "фото лічильника" in msg.answers[0]


async def test_menu_button_meters_lists_stored_readings(
    engine, providers, session, monkeypatch
):
    from dvoretskyi.db.models import MeterReading, MeterStatus

    async def empty_fetch():  # force the local-journal path
        return []

    monkeypatch.setattr(bot_app, "fetch_infolviv_readings", empty_fetch)
    gas = providers["Газ (постачання)"]
    session.add(
        MeterReading(
            provider_id=gas.id,
            cycle="2026-06",
            value=Decimal("1877.78"),
            status=MeterStatus.validated,
            created_at=clock.now(),
        )
    )
    await session.commit()

    msg = FakeMessage()
    await bot_app.menu_meters(msg)
    out = msg.answers[0]
    assert "Газ (постачання)" in out
    assert "1877.78" in out
    assert "червень 2026" in out  # cycle rendered as a Ukrainian month


def test_format_cycle_unit():
    assert bot_app._format_cycle("2026-06") == "червень 2026"
    assert bot_app._format_cycle("garbage") == "garbage"


async def test_menu_button_hello_greets(engine):
    msg = FakeMessage()
    await bot_app.menu_hello(msg)  # tap «🎩 Привіт»
    assert msg.answers and msg.answers[0] in bot_app._GREETINGS


async def test_menu_button_unpaid_routes_like_command(engine, providers):
    msg = FakeMessage()
    await bot_app.menu_unpaid(msg)  # tap «📋 Що відкрито»
    assert msg.answers[0].startswith("Відкрите цього місяця:")


async def test_menu_button_help_routes_like_command(engine):
    msg = FakeMessage()
    await bot_app.menu_help(msg)  # tap «❓ Довідка»
    assert "/unpaid" in msg.answers[0]


def test_format_unpaid_mentions_mobile_autopay():
    # All tracked paid, but mobile's scheduled charge hasn't happened → don't claim
    # "все оплачено"; mention the auto-payment instead.
    out = _format_unpaid(
        {"all_clear": True, "open": [], "auto_pending": [{"provider": "Мобільний"}]}
    )
    assert "Мобільний" in out and "автосписанням" in out and "20-го" in out
    assert "цього місяця все оплачено" not in out


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
