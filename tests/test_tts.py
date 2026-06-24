from __future__ import annotations

from dvoretskyi.agent import tts as tts_mod
from dvoretskyi.agent.tts import (
    NullTTSProvider,
    PiperTTSProvider,
    get_tts_provider,
    voiceify,
)
from dvoretskyi.config import get_settings

# --- voiceify: screen text → clean spoken Ukrainian -------------------------


def test_voiceify_strips_emoji_and_speaks_symbols():
    # A typical confirmation: emoji + ₴ → no emoji, «гривень» spoken.
    assert voiceify("✅ Світло — 420 ₴, закрито. 🎩") == "Світло — 420 гривень, закрито."


def test_voiceify_folds_lines_into_sentences():
    # A multi-line emoji-bulleted self-description voices as flowing sentences, no markup.
    out = voiceify("💸 Гроші — записую\n🔢 Показники — читаю з фото")
    assert out == "Гроші — записую. Показники — читаю з фото"
    assert "💸" not in out and "•" not in out


def test_voiceify_drops_arrows_and_markup():
    assert voiceify("↪ Це інше житло") == "Це інше житло"
    assert voiceify("**гроші** і `код`") == "гроші і код"


def test_voiceify_empty():
    assert voiceify("") == ""
    assert voiceify("   \n  ") == ""


# --- providers --------------------------------------------------------------


async def test_null_provider_never_synthesizes():
    assert await NullTTSProvider().synthesize("привіт") is None


async def test_piper_returns_none_without_a_voice_model(monkeypatch):
    # Default config has an empty piper_voice → no synth (text reply), and crucially we
    # never even try to spawn the binary. Deploying before the model is installed is safe.
    get_settings.cache_clear()
    monkeypatch.setenv("PIPER_VOICE", "")
    spawned: list = []

    async def fake_spawn(*a, **k):
        spawned.append(a)
        return True

    monkeypatch.setattr(PiperTTSProvider, "_spawn", staticmethod(fake_spawn))
    assert await PiperTTSProvider().synthesize("щось") is None
    assert spawned == []  # never reached the subprocess
    get_settings.cache_clear()


async def test_piper_skips_overly_long_text(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("PIPER_VOICE", "/some/voice.onnx")
    monkeypatch.setenv("TTS_MAX_CHARS", "20")
    spawned: list = []

    async def fake_spawn(*a, **k):
        spawned.append(a)
        return True

    monkeypatch.setattr(PiperTTSProvider, "_spawn", staticmethod(fake_spawn))
    assert await PiperTTSProvider().synthesize("a" * 50) is None
    assert spawned == []  # too long → fall back to text, don't synth
    get_settings.cache_clear()


def test_get_tts_provider_honours_none(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("TTS_PROVIDER", "none")
    assert isinstance(get_tts_provider(), NullTTSProvider)
    get_settings.cache_clear()
    monkeypatch.setenv("TTS_PROVIDER", "piper")
    assert isinstance(get_tts_provider(), PiperTTSProvider)
    get_settings.cache_clear()


def test_module_uses_a_private_temp_dir():
    # Synth scratch files live in a dedicated dir (created mode-restricted), never logged.
    assert str(tts_mod.PiperTTSProvider._DIR).endswith("dvoretskyi_tts")
