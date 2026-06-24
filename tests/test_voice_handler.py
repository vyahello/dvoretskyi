from __future__ import annotations

from dvoretskyi.bot import app as bot_app
from tests.conftest import FakeTranscriptionProvider


class FakeVoiceMessage:
    """Duck-typed Message: has a voice note, records answers (text + keyboard)."""

    def __init__(self) -> None:
        self.voice = object()  # truthy; download is monkeypatched
        self.bot = object()
        self.answers: list[tuple[str, object]] = []

    async def answer(self, text: str, reply_markup=None, **kw) -> None:
        self.answers.append((text, reply_markup))


def _patch_voice(monkeypatch, transcript: str) -> FakeTranscriptionProvider:
    stt = FakeTranscriptionProvider(transcript)

    async def fake_download(message):
        return "/tmp/dvoretskyi_fake_voice.ogg"

    monkeypatch.setattr(bot_app, "_download_voice", fake_download)
    monkeypatch.setattr(bot_app, "get_transcription_provider", lambda: stt)
    return stt


async def test_voice_routes_transcript_without_echoing_it(engine, monkeypatch):
    stt = _patch_voice(monkeypatch, "що треба заплатити?")

    captured: dict = {}

    async def fake_respond(message, text, **kwargs):
        captured["text"] = text
        captured["voice_reply"] = kwargs.get("voice_reply")
        await message.answer("(відповідь агента)")

    monkeypatch.setattr(bot_app, "_respond_to_text", fake_respond)

    msg = FakeVoiceMessage()
    await bot_app.on_voice(msg)

    assert stt.calls, "transcription should have run once"
    # The transcript is routed to the agent (which sends its own «I'm on it» line)…
    assert captured["text"] == "що треба заплатити?"
    # …a voice ask is answered by voice…
    assert captured["voice_reply"] is True
    # …and the bot never echoes the user's words back verbatim.
    assert all("🎙 Почув" not in text for text, _ in msg.answers)


async def test_voice_unintelligible_asks_to_retry(engine, monkeypatch):
    _patch_voice(monkeypatch, "")  # empty transcript = could not understand

    routed: list[str] = []

    async def fake_respond(message, text, **kwargs):
        routed.append(text)

    monkeypatch.setattr(bot_app, "_respond_to_text", fake_respond)

    msg = FakeVoiceMessage()
    await bot_app.on_voice(msg)

    assert not routed, "an empty transcript must never reach the agent"
    assert "Не розчув" in msg.answers[-1][0]


async def test_voice_disabled_tells_user_to_type(engine, monkeypatch):
    from dvoretskyi.config import get_settings

    monkeypatch.setattr(get_settings(), "stt_provider", "none")

    msg = FakeVoiceMessage()
    await bot_app.on_voice(msg)

    assert "текстом" in msg.answers[-1][0]
