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
    assert (
        voiceify("✅ Світло — 420 ₴, закрито. 🎩")
        == "Світло, чотириста двадцять гривень, закрито."
    )


def test_voiceify_money_kopiyky_and_plural():
    assert voiceify("510.00 ₴") == "п'ятсот десять гривень"  # whole → drop the .00
    assert voiceify("510.10 ₴") == "п'ятсот десять гривень десять копійок"
    # Thousands de-grouped; 2391 ends in 1 → feminine «одна гривня», 39 → «копійок».
    # espeak alone would say «два тисяча триста дев'яностооден гривня» — wrong gender,
    # undeclined «тисяча», fused words. Hence `_int_words`.
    assert (
        voiceify("разом 2 391.39 ₴")
        == "разом дві тисячі триста дев'яносто одна гривня тридцять дев'ять копійок"
    )
    assert voiceify("1 ₴") == "одна гривня"  # feminine, not espeak's «оден»
    assert voiceify("2 ₴") == "дві гривні"
    assert voiceify("0.50 ₴") == "п'ятдесят копійок"
    assert voiceify("21.01 ₴") == "двадцять одна гривня одна копійка"


def test_int_words_gender_and_magnitudes():
    # «тисяча» is feminine regardless of what's counted; the remainder takes the unit's
    # gender. espeak gets none of this right, which is why we spell numerals out.
    assert tts_mod._int_words(0) == "нуль"
    assert tts_mod._int_words(1) == "один"
    assert tts_mod._int_words(1, fem=True) == "одна"
    assert tts_mod._int_words(2, fem=True) == "дві"
    assert tts_mod._int_words(14) == "чотирнадцять"  # teens carry no gender
    assert tts_mod._int_words(14, fem=True) == "чотирнадцять"
    assert tts_mod._int_words(1000) == "одна тисяча"
    assert tts_mod._int_words(2026) == "дві тисячі двадцять шість"
    assert tts_mod._int_words(1888) == "одна тисяча вісімсот вісімдесят вісім"
    assert tts_mod._int_words(100000) == "сто тисяч"  # not espeak's «сто тисяча»
    assert tts_mod._int_words(1000000) == "один мільйон"  # espeak drops the magnitude
    assert tts_mod._int_words(21, fem=True) == "двадцять одна"
    assert tts_mod._int_words(391, fem=True) == "триста дев'яносто одна"
    # Out of any real range → left to espeak rather than mis-spelled.
    assert tts_mod._int_words(1_000_000_000) == "1000000000"


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
    assert (
        voiceify("показник 1888.14")
        == "показник одна тисяча вісімсот вісімдесят вісім кома чотирнадцять"
    )
    # Leading zeros are voiced («нуль»), so «3.03» isn't read as «3.3».
    assert voiceify("3.03") == "три кома нуль три"
    assert voiceify("5.007") == "п'ять кома нуль нуль сім"


def test_voiceify_reads_dotted_code_as_pauses():
    # A login/contract number «00.28.00.36» reads as pause-separated groups, never
    # «00 крапка 28 крапка …» (espeak voices a literal dot as «крапка»).
    assert voiceify("Логін (договір): 00.28.00.36.") == "Логін договір: 00, 28, 00, 36."
    assert voiceify("договір 12.34.56") == "договір 12, 34, 56"
    # A genuine decimal has a single dot → still spoken «кома», not split into groups.
    assert (
        voiceify("показник 1888.14")
        == "показник одна тисяча вісімсот вісімдесят вісім кома чотирнадцять"
    )


def test_voiceify_reads_bare_digit_login_digit_by_digit():
    # A bare digit identifier (the Gigabit+ login/contract has no separators) — espeak
    # would read «00280036» as a grouped cardinal and voice «00 крапка 280 крапка 036».
    # Read it digit-by-digit so each digit (incl. the leading zeros) is spoken cleanly.
    assert voiceify("Логін (договір): 00280036.") == "Логін договір: 0 0 2 8 0 0 3 6."
    assert voiceify("договір 12345678") == "договір 1 2 3 4 5 6 7 8"
    # A short number / year we don't own is left to espeak (its number words are
    # stress-patched via the `_N` entries in scripts/uk_stress_overrides.txt).
    assert voiceify("за 2026 рік") == "за 2026 рік"
    assert voiceify("420 разів") == "420 разів"
    # A money amount is spelled out, so there is no digit run left to split up.
    assert voiceify("разом 100000 ₴") == "разом сто тисяч гривень"


def test_voiceify_meter_volume_named_with_unit():
    # «м³» is spoken as a declined «кубометр», so a reading isn't a bare unitless number.
    assert (
        voiceify("• червень 2026: 1888.14 м³ (спожито 3.03 м³)")
        == "червень дві тисячі двадцять шостого року: одна тисяча вісімсот вісімдесят "
        "вісім кома чотирнадцять кубометра спожито три кома нуль три кубометра"
    )
    assert voiceify("5 м³") == "п'ять кубометрів"
    # «кубометр» is masculine → «один», never the feminine «одна» money gets.
    assert voiceify("1 м³") == "один кубометр"
    assert voiceify("2 м³") == "два кубометри"


def test_voiceify_percent_is_spelled_out_and_declined():
    assert voiceify("частка 21%") == "частка двадцять один відсоток"
    assert voiceify("частка 5%") == "частка п'ять відсотків"


def test_voiceify_speaks_latin_brand_terms():
    # espeak's uk voice mangles a Latin word («monobank» → «монобайк») — give it a spoken
    # Ukrainian form. Case-insensitive; «Gigabit+» wins over the «gigabit» prefix.
    assert voiceify("автосписанням monobank 20-го") == "автосписанням монобанк двадцятого"
    assert voiceify("autopay monobank усе зробить") == "автосписання монобанк усе зробить"
    assert voiceify("Інтернет (Gigabit+)") == "Інтернет гігабіт плюс"


def test_voiceify_reads_slash_between_words_as_abo():
    # espeak reads the glyph aloud: «газу/води» → «газу коса риска води». Between two
    # Ukrainian words a slash is spoken «або».
    assert voiceify("покажи показники газу/води") == "покажи показники газу або води"
    assert voiceify("немає логіна/пароля") == "немає логіна або пароля"
    # Non-Cyrillic on either side is left alone — a ratio, a unit or a path is not «або».
    assert voiceify("24/7") == "24/7"
    assert voiceify("шлях /tmp/x") == "шлях /tmp/x"


def test_voiceify_signed_percent_keeps_its_direction():
    """The stats delta is «▲ +8% до травня». The arrow is a pictograph and gets stripped,
    so the SIGN is the only thing carrying the direction — and the old line-strip
    (`ln.strip(" -–—·•")`) ATE a leading minus, voicing a DROP as a bare «тринадцять
    відсотків». Half the comparisons meant the opposite of what was said."""
    assert "менше на тринадцять відсотків" in voiceify("▼ -13% до червня")
    assert "більше на вісім відсотків" in voiceify("▲ +8% до травня")
    assert "▼" not in voiceify("▼ -13% до червня")  # the triangle is never read aloud
    # A bullet dash is still decoration and still goes.
    assert voiceify("- Газ") == "Газ"


def test_voiceify_reads_our_own_dotted_dates_as_dates():
    """The bot writes «06.06.2026» (payment journal) and «подано 28.06» (meter journal).

    The dotted-code rule claimed the first — «коли я платив за газ» came back as
    «нуль шість, нуль шість, дві тисячі двадцять шість», a date read as a contract
    number — and the bare dd.mm fell through to the decimal pass.
    """
    assert voiceify("• 06.06.2026 — 480.00 ₴") == (
        "шостого червня дві тисячі двадцять шостого року, чотириста вісімдесят гривень"
    )
    assert "подано двадцять восьмого червня" in voiceify("· подано 28.06")
    # A meter volume is NOT a date, even though «3.03» parses as day 3, month 03 — which
    # is exactly why the dd.mm rule is anchored on «подано».
    assert "кубометра" in voiceify("спожито 3.03 м³")


def test_voiceify_expands_screen_abbreviations_and_per_month():
    # «сер.»/«міс.» are read as the words «сер»/«міс»; «м³/міс» is «per», not «or».
    out = voiceify("сер. 41.2 м³/міс")
    assert "середнє" in out and "на місяць" in out
    assert "або" not in out  # the generic slash rule must not claim «/міс»
    assert "8 місяців" in voiceify("8 міс. по місяцях")


def test_voiceify_middot_becomes_a_pause():
    # «·» separates fields on screen; a voice must neither read it nor weld the fields.
    out = voiceify("Житло 2 · липень 2026")
    assert "·" not in out and "крапка" not in out
    assert out.startswith("Житло 2,")


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


async def test_piper_forwards_custom_espeak_data(monkeypatch):
    # An explicit PIPER_ESPEAK_DATA (our stress-override dir) is passed to piper as
    # --espeak_data; with it empty and no default dir built, the flag is absent.
    get_settings.cache_clear()
    monkeypatch.setenv("PIPER_VOICE", "/some/voice.onnx")
    monkeypatch.setenv("PIPER_ESPEAK_DATA", "/opt/uk-stress/espeak-ng-data")
    captured: list = []

    async def fake_spawn(args, *a, **k):
        captured.append(args)
        # Pretend piper wrote a non-empty WAV and ffmpeg produced the OGG.
        if args[0].endswith("ffmpeg"):
            import pathlib

            pathlib.Path(args[-1]).write_bytes(b"x")
        else:
            import pathlib

            pathlib.Path(args[args.index("--output_file") + 1]).write_bytes(b"x")
        return True

    monkeypatch.setattr(PiperTTSProvider, "_spawn", staticmethod(fake_spawn))
    await PiperTTSProvider().synthesize("привіт")
    piper_call = next(c for c in captured if not c[0].endswith("ffmpeg"))
    assert "--espeak_data" in piper_call
    assert piper_call[piper_call.index("--espeak_data") + 1] == (
        "/opt/uk-stress/espeak-ng-data"
    )
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
