from __future__ import annotations

import os
from decimal import Decimal

from dvoretskyi import clock
from dvoretskyi.bot import app as bot_app
from dvoretskyi.bot.app import (
    _format_stats,
    _format_unpaid,
    _household_suffix,
    _payee_hint,
    cmd_help,
    cmd_start,
    cmd_stats,
    cmd_unpaid,
    on_text,
)
from dvoretskyi.db.models import (
    Category,
    PayChannel,
    Payment,
    PaymentSource,
    Provider,
)


class FakeMessage:
    """Duck-typed aiogram Message that records what the handler sent."""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[str] = []
        self.photos: list[tuple[object, str | None]] = []
        self.voices: list[tuple[object, object]] = []
        self.markups: list[object] = []

    async def answer(self, text: str, **kw) -> None:
        self.answers.append(text)
        self.markups.append(kw.get("reply_markup"))

    async def answer_photo(self, photo, caption: str | None = None, **kw) -> None:
        self.photos.append((photo, caption))

    async def answer_voice(self, voice, reply_markup=None, **kw) -> None:
        self.voices.append((voice, reply_markup))


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw) -> None:
        self.sent.append((text, reply_markup))


class _FakeCallback:
    """Duck-typed CallbackQuery; .message isn't an aiogram Message, so `_edit` routes
    through bot.send_message (recorded in `.bot.sent`)."""

    def __init__(self, data: str) -> None:
        self.data = data
        self.bot = _FakeBot()
        self.message = type("M", (), {"chat": type("C", (), {"id": 1})()})()
        self.answered = False

    async def answer(self, text: str | None = None, **kw) -> None:
        self.answered = True


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
    # Steers to the buttons + natural language, not slash commands.
    assert "Довідка" in text and "дворецький" in text.lower()
    assert "/help" not in text


async def test_cmd_help(engine):
    msg = FakeMessage()
    await cmd_help(msg)
    text = msg.answers[0]
    # The help now points at the on-screen buttons, not «/команди».
    for label in ("Що сплатити", "Статистика", "Мої показники", "Довідка"):
        assert label in text
    assert "/unpaid" not in text and "/stats" not in text
    assert "заплатити" in text  # free-text hint
    assert "фото лічильника" in text  # how to file a reading
    # Voice is now emphasised, and the new buttons are documented.
    assert "ГОЛОСОМ" in text and "🎙" in text
    assert "Як платити" in text


async def test_cmd_start_mentions_voice(engine):
    msg = FakeMessage()
    await cmd_start(msg)
    assert "голос" in msg.answers[0].lower()


async def test_menu_payplan_shows_plan_with_links(engine, providers):
    from dvoretskyi.bot.app import menu_payplan

    msg = FakeMessage()
    await menu_payplan(msg)
    text = msg.answers[0]
    assert "Як і коли платимо" in text
    assert "monobank" in text and "ДАХ" in text  # method per provider
    # Pay links ride as an inline keyboard.
    markup = msg.markups[0]
    assert markup is not None and markup.inline_keyboard


async def test_history_button_opens_a_chooser_menu(engine, providers):
    from dvoretskyi.bot.app import menu_history

    msg = FakeMessage()
    await menu_history(msg)
    # «📜 Історія» no longer dumps everything — it offers a 2-button chooser.
    assert "Що показати" in msg.answers[0]
    markup = msg.markups[0]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Показники" in x for x in labels) and any("Платежі" in x for x in labels)


async def test_history_nav_readings_and_payments(engine, providers):
    from dvoretskyi.bot.app import on_history_nav
    from dvoretskyi.db.models import MeterReading, MeterStatus

    gas = providers["Газ (постачання)"]
    async with bot_app.session_scope() as s:
        s.add(_payment(gas.id, "480.00", "hist1"))
        s.add(
            MeterReading(
                provider_id=gas.id,
                cycle=clock.current_cycle(),
                value=Decimal("1888.14"),
                status=MeterStatus.submitted,
                created_at=clock.now(),
                submitted_at=clock.now(),
            )
        )
        await s.commit()

    # Readings view — edits in place, carries a «⬅️ Назад» button.
    cb = _FakeCallback("h:met")
    await on_history_nav(cb)
    text, kb = cb.bot.sent[-1]
    assert "Історія показників" in text
    assert any("Назад" in b.text for row in kb.inline_keyboard for b in row)

    # Payments → two households → a chooser first, then the primary's payments.
    cb = _FakeCallback("h:pay")
    await on_history_nav(cb)
    text, _ = cb.bot.sent[-1]
    assert "за яким житлом" in text

    cb = _FakeCallback("h:pay:primary")
    await on_history_nav(cb)
    text, kb = cb.bot.sent[-1]
    assert "Історія платежів" in text and "480.00" in text
    assert any("Назад" in b.text for row in kb.inline_keyboard for b in row)


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
    # Phrasing is randomized for liveliness, but it's always a ✅ "all clear" line.
    assert msg.answers[0].startswith("✅")
    assert msg.answers[0] in bot_app._ALL_CLEAR_LINES


def test_format_unpaid_all_clear_unit():
    out = _format_unpaid({"all_clear": True, "open": []})
    assert out.startswith("✅") and out in bot_app._ALL_CLEAR_LINES


# --- main-menu reply keyboard ----------------------------------------------


def test_main_keyboard_has_menu_buttons():
    from dvoretskyi.bot import keyboards

    labels = [b.text for row in keyboards.main_keyboard().keyboard for b in row]
    for lbl in (
        keyboards.MENU_UNPAID,
        keyboards.MENU_STATS,
        keyboards.MENU_BALANCE,
        keyboards.MENU_METERS,
        keyboards.MENU_HISTORY,
        keyboards.MENU_HELP,
    ):
        assert lbl in labels
    assert "🎩 Привіт" not in labels  # the greeting button was dropped


def test_journal_photo_buttons_only_for_saved_photos():
    from dvoretskyi.bot import app as bot_app

    sections = [
        {
            "provider": "Газ (постачання)",
            "readings": [
                # photo_id may differ from the displayed id (photo on a sibling row).
                {"id": 2, "photo_id": 5, "cycle": "2026-06", "has_photo": True},
                {"id": 8, "photo_id": None, "cycle": "2026-05", "has_photo": False},
            ],
        },
        {
            "provider": "Холодна вода",
            "readings": [{"id": 1, "photo_id": 6, "cycle": "2026-06", "has_photo": True}],
        },
    ]
    items = bot_app._journal_photo_buttons(sections)
    # The button carries the photo_id (the row whose file survives), not the display id.
    assert [rid for rid, _ in items] == [5, 6]
    assert items[0][1] == "📸 Газ (постачання) · червень 2026"  # names meter + month


def test_meter_photo_keyboard():
    from dvoretskyi.bot import keyboards

    kb = keyboards.meter_photo_keyboard([(7, "📸 Газ · червень 2026")])
    assert kb is not None
    assert kb.inline_keyboard[0][0].callback_data == "mp:7"
    assert keyboards.meter_photo_keyboard([]) is None  # nothing saved → plain message


async def test_menu_button_meters_shows_infolviv_when_available(engine, monkeypatch):
    from dvoretskyi.agent.infolviv import InfolvivReading

    async def fake_fetch():
        return [
            InfolvivReading(
                kind="water",
                account_code="ACC-WATER-1",  # fake рахунок
                counter_number="10000001",  # fake — never a real account
                provider="ВОДОКАНАЛ (тест)",
                service="Централізоване водопостачання (ХВ)",
                period="2026-05",
                value=Decimal("100.500"),
                difference=Decimal("0.0"),
                window_start_day=1,
                window_end_day=10,
                window_open=True,
                counter_id=111,
            )
        ]

    monkeypatch.setattr(bot_app, "fetch_infolviv_readings", fake_fetch)
    msg = FakeMessage()
    await bot_app.menu_meters(msg)
    out = msg.answers[0]
    assert "infolviv" in out
    assert "Холодна вода" in out and "100.500" in out
    # рахунок shown (in a <code> span so Telegram won't auto-link it), not the serial.
    assert "№<code>ACC-WATER-1</code>" in out
    assert "10000001" not in out  # the physical serial is never displayed
    assert "травень 2026" in out
    assert "число місяця" in out  # end-of-month submission window


def test_submission_window_label_is_end_of_month():
    from datetime import datetime

    from dvoretskyi import clock

    # June 2026 has 30 days; with meter_window_days=3 → «28–30 число місяця».
    june = datetime(2026, 6, 15, tzinfo=clock.KYIV)
    assert bot_app._submission_window_label(june) == "28–30 число місяця"


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


async def test_menu_button_history_shows_journal(engine, providers):
    # «📜 Історія» now opens a chooser; the empty readings journal lives one tap deeper.
    msg = FakeMessage()
    await bot_app.menu_history(msg)  # tap «📜 Історія»
    assert msg.answers and "Що показати" in msg.answers[0]
    cb = _FakeCallback("h:met")
    await bot_app.on_history_nav(cb)  # → readings view
    text, _ = cb.bot.sent[-1]
    assert "журнал чистий" in text  # empty journal still answers cleanly
    # No greeting button/handler remains.
    assert not hasattr(bot_app, "menu_hello")


async def test_menu_button_unpaid_routes_like_command(engine, providers):
    msg = FakeMessage()
    await bot_app.menu_unpaid(msg)  # tap «📋 Що відкрито»
    assert msg.answers[0].startswith("Відкрите цього місяця:")


async def test_menu_button_help_routes_like_command(engine):
    msg = FakeMessage()
    await bot_app.menu_help(msg)  # tap «❓ Довідка»
    assert msg.answers[0] == bot_app.HELP_TEXT


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
    # Caption now reads in words («червень 2026»), not the raw «2026-06» cycle key.
    assert "480.00" in caption and "червень" in caption

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


async def test_on_text_passes_a_progress_callback(engine, monkeypatch):
    # Typed asks get the same natural «I'm on it» line as voice — on_text must hand the
    # dispatcher an on_progress callback that sends a message.
    from dvoretskyi.agent.dispatcher import Reply

    captured: dict = {}

    async def fake_handle(user_text, session, llm, *, history=None, on_progress=None):
        captured["on_progress"] = on_progress
        if on_progress is not None:
            await on_progress("Гляну, що ще відкрито…")
        return Reply(text="Відкрите: вода.")

    monkeypatch.setattr(bot_app.agent_dispatcher, "handle_message", fake_handle)

    msg = FakeMessage(text="що треба заплатити?")
    await on_text(msg)

    assert callable(captured["on_progress"])
    # the progress line lands first, the answer second
    assert msg.answers[0] == "Гляну, що ще відкрито…"
    assert "Відкрите: вода." in msg.answers[-1]


async def test_voice_ask_gets_a_voice_reply(engine, monkeypatch, tmp_path):
    # A voice question is answered with a synthesized voice note (no text bubble), and the
    # transient OGG is cleaned up after sending.
    from dvoretskyi.agent.dispatcher import Reply

    async def fake_handle(user_text, session, llm, *, history=None, on_progress=None):
        return Reply(text="Світло закрив — 420 ₴. 🎩")

    monkeypatch.setattr(bot_app.agent_dispatcher, "handle_message", fake_handle)

    ogg = tmp_path / "reply.ogg"
    ogg.write_bytes(b"OggS-fake-audio")

    class FakeTTS:
        async def synthesize(self, text: str) -> str:
            return str(ogg)

    monkeypatch.setattr(bot_app, "get_tts_provider", lambda: FakeTTS())

    msg = FakeMessage()
    await bot_app._respond_to_text(msg, "що там зі світлом?", voice_reply=True)

    assert len(msg.voices) == 1  # spoken reply sent
    assert not msg.answers  # no duplicate text bubble
    assert not ogg.exists()  # transient audio removed after sending


async def test_voice_synth_failure_falls_back_to_text(engine, monkeypatch):
    # If synth yields nothing (disabled / no model / error), the voice asker still gets
    # the answer as text — never dead air.
    from dvoretskyi.agent.dispatcher import Reply

    async def fake_handle(user_text, session, llm, *, history=None, on_progress=None):
        return Reply(text="Усе закрито.")

    monkeypatch.setattr(bot_app.agent_dispatcher, "handle_message", fake_handle)

    class FakeTTS:
        async def synthesize(self, text: str) -> None:
            return None

    monkeypatch.setattr(bot_app, "get_tts_provider", lambda: FakeTTS())

    msg = FakeMessage()
    await bot_app._respond_to_text(msg, "усе оплачено?", voice_reply=True)

    assert not msg.voices
    assert msg.answers == ["Усе закрито."]


async def test_voice_turn_suppresses_text_progress_line(engine, monkeypatch, tmp_path):
    # On a voice ask the «записує аудіо…» header is the ack; the «I'm on it» progress line
    # must NOT also be posted as a text bubble before the voice reply.
    from dvoretskyi.agent.dispatcher import Reply

    async def fake_handle(user_text, session, llm, *, history=None, on_progress=None):
        if on_progress is not None:
            await on_progress("Збираю статистику…")  # a text bubble for a TYPED ask
        return Reply(text="За червень — 460 ₴.")

    monkeypatch.setattr(bot_app.agent_dispatcher, "handle_message", fake_handle)

    ogg = tmp_path / "r.ogg"
    ogg.write_bytes(b"OggS")

    class FakeTTS:
        async def synthesize(self, text: str) -> str:
            return str(ogg)

    monkeypatch.setattr(bot_app, "get_tts_provider", lambda: FakeTTS())

    msg = FakeMessage()
    await bot_app._respond_to_text(msg, "скільки за газ?", voice_reply=True)

    assert len(msg.voices) == 1  # only the spoken answer
    assert msg.answers == []  # the «Збираю статистику…» line was not sent as text


async def test_voice_send_refused_falls_back_to_text(engine, monkeypatch, tmp_path):
    # Telegram can refuse a voice note (e.g. VOICE_MESSAGES_FORBIDDEN — the recipient's
    # privacy setting). The send error must fall back to text, never leave the user empty.
    from dvoretskyi.agent.dispatcher import Reply

    async def fake_handle(user_text, session, llm, *, history=None, on_progress=None):
        return Reply(text="Усе закрито.")

    monkeypatch.setattr(bot_app.agent_dispatcher, "handle_message", fake_handle)

    ogg = tmp_path / "reply.ogg"
    ogg.write_bytes(b"OggS-fake-audio")

    class FakeTTS:
        async def synthesize(self, text: str) -> str:
            return str(ogg)

    monkeypatch.setattr(bot_app, "get_tts_provider", lambda: FakeTTS())

    class RefusingMessage(FakeMessage):
        async def answer_voice(self, voice, reply_markup=None, **kw) -> None:
            raise RuntimeError(
                "Telegram server says - Bad Request: VOICE_MESSAGES_FORBIDDEN"
            )

    msg = RefusingMessage()
    await bot_app._respond_to_text(msg, "усе оплачено?", voice_reply=True)

    assert msg.answers == ["Усе закрито."]  # fell back to text
    assert not msg.voices  # the refused voice note was not recorded
    assert not ogg.exists()  # transient audio still cleaned up


def test_payee_hint_strips_phone_and_collapses_lines():
    # monobank's mobile top-up description is «Lifecell\n+380…» — the prompt should name
    # the carrier, never the bare phone number.
    assert _payee_hint("Lifecell\n+380931403184") == "Lifecell"
    assert _payee_hint("  Київстар   +380501234567 ") == "Київстар"
    # Short numbers (amounts/dates) survive — only ≥6-digit runs are noise.
    assert _payee_hint("ОСББ Зоря, кв 12") == "ОСББ Зоря, кв 12"
    assert _payee_hint("") == ""
    assert _payee_hint(None) == ""


async def test_household_suffix_omitted_for_mobile(session, households):
    # A phone top-up isn't tied to a property → no « · <житло>» suffix; a real utility
    # still gets one.
    mobile = Provider(
        name="Мобільний",
        category=Category.mobile,
        pay_channel=PayChannel.mono_card,
        household_id=households["primary"].id,
    )
    water = Provider(
        name="Холодна вода",
        category=Category.water,
        pay_channel=PayChannel.mono_communal,
        household_id=households["primary"].id,
    )
    session.add_all([mobile, water])
    await session.commit()

    assert await _household_suffix(session, mobile) == ""
    assert await _household_suffix(session, water) == " · Житло 1"
