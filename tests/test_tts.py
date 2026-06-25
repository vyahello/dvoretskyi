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


def test_voiceify_strips_emoji_and_speaks_money():
    # A typical confirmation: emoji gone, dash → pause, «420 ₴» → spoken hryvnias.
    assert voiceify("✅ Світло — 420 ₴, закрито. 🎩") == "Світло, 420 гривень, закрито."


def test_voiceify_money_kopiyky_and_plural():
    assert voiceify("510.00 ₴") == "510 гривень"  # whole → drop the .00
    assert voiceify("510.10 ₴") == "510 гривень 10 копійок"  # with kopiykas
    # thousands de-grouped, and 2391 ends in 1 → «гривня», 39 → «копійок»
    assert voiceify("разом 2 391.39 ₴") == "разом 2391 гривня 39 копійок"
    assert voiceify("1 ₴") == "1 гривня"
    assert voiceify("0.50 ₴") == "50 копійок"


def test_voiceify_dates_and_ordinals():
    # Full date reads like a person: genitive-ordinal day AND year, plus «року».
    assert (
        voiceify("подав 2026-06-06")
        == "подав шостого червня дві тисячі двадцять шостого року"
    )
    # A bare period is spoken with the year in full + «року», not a flat «червень 2026».
    assert voiceify("за 2026-06") == "за червень дві тисячі двадцять шостого року"
    assert voiceify("до 20-го") == "до двадцятого"
    assert voiceify("до 31-го") == "до тридцять першого"


def test_voiceify_decimals_read_as_koma():
    assert voiceify("показник 1888.14") == "показник 1888 кома 14"
    # Leading zeros are voiced («нуль»), so «3.03» isn't read as «3.3». The non-zero
    # remainder stays as digits — espeak voices «3» as «три».
    assert voiceify("3.03") == "3 кома нуль 3"
    assert voiceify("5.007") == "5 кома нуль нуль 7"


def test_voiceify_meter_volume_named_with_unit():
    # «м³» is spoken as a declined «кубометр», so a reading isn't a bare unitless number.
    assert (
        voiceify("• червень 2026: 1888.14 м³ (спожито 3.03 м³)")
        == "червень дві тисячі двадцять шостого року: 1888 кома 14 кубометра "
        "спожито 3 кома нуль 3 кубометра"
    )
    assert voiceify("5 м³") == "5 кубометрів"
    assert voiceify("1 м³") == "1 кубометр"


def test_voiceify_stress_hints_are_opt_in():
    # Off (default): plain text, no accents — so the existing voiceify contract holds.
    assert "́" not in voiceify("показник за червень: 510 гривень")
    # On: the stressed vowel of known domain words is marked (U+0301 sits after it), and
    # capitalization is preserved.
    out = voiceify("Показник за червень: 510 гривень", stress_hints=True)
    assert "Показни́к" in out
    assert "че́рвень" in out
    assert "гри́вень" in out


def test_voiceify_folds_lines_into_sentences():
    # A multi-line emoji-bulleted self-description voices as flowing sentences, no markup.
    out = voiceify("💸 Гроші — записую\n🔢 Показники — читаю з фото")
    assert out == "Гроші, записую. Показники, читаю з фото"
    assert "💸" not in out and "•" not in out


def test_voiceify_drops_arrows_quotes_and_markup():
    assert voiceify("↪ Це інше житло") == "Це інше житло"
    assert voiceify("**гроші** і `код`") == "гроші і код"
    # quotes/brackets are dropped, not read aloud as «лапки»/«дужки»
    assert voiceify("«Lifecell»") == "Lifecell"
    assert voiceify("Газ (постачання)") == "Газ постачання"


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
