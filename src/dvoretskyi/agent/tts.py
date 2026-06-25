"""Text-to-speech for voice replies, behind a `TTSProvider` abstraction.

The mirror image of `transcription.py`: the default `PiperTTSProvider` drives the local
Piper neural TTS — an external binary (like `claude_bin`), so there is no extra pip
dependency and synthesized audio never leaves the box. The produced OGG/Opus file is sent
then deleted, exactly like a meter photo; audio bytes are never logged.

Replies are written for the screen (emoji, ₴, bullet lists). `voiceify` strips that down
to clean spoken Ukrainian before synthesis. On any failure the contract is None — the bot
then just sends the text reply, so a synth hiccup never costs the user the answer. Same
when no voice model is configured, so deploying before the model is installed is safe.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from dvoretskyi.config import Settings, get_settings

log = logging.getLogger(__name__)

# Stress-override espeak data dir (built by scripts/build_espeak_stress.py). Convention
# mirrors METER_PHOTO_DIR: an empty PIPER_ESPEAK_DATA falls back to this default path if a
# uk_dict has actually been compiled there — so the fix turns on the moment the dir exists
# (next voice reply), with no .env edit and no restart. An explicit setting always wins.
_DEFAULT_ESPEAK_DATA = Path.home() / ".dvoretskyi" / "espeak-ng-data"


def _espeak_data_dir(s: Settings) -> str:
    """The espeak data dir to hand Piper, or '' to let it use its bundled default."""
    if s.piper_espeak_data:
        return s.piper_espeak_data
    if (_DEFAULT_ESPEAK_DATA / "uk_dict").exists():
        return str(_DEFAULT_ESPEAK_DATA)
    return ""


# Emoji / pictographs / arrows / symbols that a voice would read as "галочка", "стрілка",
# … — strip them. ₴/numbers/dates are handled by the spoken-form passes below.
_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff"  # pictographs, transport, supplemental, flags (1F1E6–1F1FF)
    "←-⇿"  # arrows (e.g. ↪ «Це інше житло»)
    "⌀-⏿"  # misc technical (⏳ ⏰)
    "☀-➿"  # misc symbols + dingbats (✅ ✂)
    "⬀-⯿"  # extra arrows/stars
    "️‍]"  # variation selector + zero-width joiner
)
# Quotes/brackets a voice reads aloud («відкрити лапки», «дужки») — drop them.
_STRIP_RE = re.compile('[«»„“”"‟‘’‚‛‹›`´()\\[\\]{}]')
# Markdown-ish punctuation the persona avoids but might still slip in.
_MARKUP_RE = re.compile(r"[*_#>|]")

_MONTHS_GEN: dict[int, str] = {
    1: "січня",
    2: "лютого",
    3: "березня",
    4: "квітня",
    5: "травня",
    6: "червня",
    7: "липня",
    8: "серпня",
    9: "вересня",
    10: "жовтня",
    11: "листопада",
    12: "грудня",
}
_MONTHS_NOM: dict[int, str] = {
    1: "січень",
    2: "лютий",
    3: "березень",
    4: "квітень",
    5: "травень",
    6: "червень",
    7: "липень",
    8: "серпень",
    9: "вересень",
    10: "жовтень",
    11: "листопад",
    12: "грудень",
}
# Genitive ordinals, so dates read the way a person says them («шостого червня дві
# тисячі двадцять шостого року») rather than digit-by-digit.
_ORD_1_20: dict[int, str] = {
    1: "першого",
    2: "другого",
    3: "третього",
    4: "четвертого",
    5: "п'ятого",
    6: "шостого",
    7: "сьомого",
    8: "восьмого",
    9: "дев'ятого",
    10: "десятого",
    11: "одинадцятого",
    12: "дванадцятого",
    13: "тринадцятого",
    14: "чотирнадцятого",
    15: "п'ятнадцятого",
    16: "шістнадцятого",
    17: "сімнадцятого",
    18: "вісімнадцятого",
    19: "дев'ятнадцятого",
    20: "двадцятого",
}
_TENS_ORD: dict[int, str] = {
    20: "двадцятого",
    30: "тридцятого",
    40: "сорокового",
    50: "п'ятдесятого",
    60: "шістдесятого",
    70: "сімдесятого",
    80: "вісімдесятого",
    90: "дев'яностого",
}
_TENS_CARD: dict[int, str] = {
    20: "двадцять",
    30: "тридцять",
    40: "сорок",
    50: "п'ятдесят",
    60: "шістдесят",
    70: "сімдесят",
    80: "вісімдесят",
    90: "дев'яносто",
}


def _two_ord_gen(n: int) -> str:
    """1–99 as a genitive ordinal: 6→«шостого», 26→«двадцять шостого», 30→«тридцятого»."""
    if n in _ORD_1_20:
        return _ORD_1_20[n]
    tens, unit = (n // 10) * 10, n % 10
    if tens not in _TENS_CARD:
        return str(n)
    return _TENS_ORD[tens] if unit == 0 else f"{_TENS_CARD[tens]} {_ORD_1_20[unit]}"


def _year_ord_gen(y: int) -> str:
    """A 21st-century year as a genitive ordinal: 2026 → «дві тисячі двадцять шостого»."""
    if y == 2000:
        return "двохтисячного"
    if 2001 <= y <= 2099:
        return "дві тисячі " + _two_ord_gen(y % 100)
    return str(y)


def _ua_plural(n: int, one: str, few: str, many: str) -> str:
    """Ukrainian plural agreement: 1 гривня / 2-4 гривні / 5+ гривень (with the 11-14
    exception). The numeral stays as digits — espeak-ng voices them in Ukrainian."""
    n = abs(n)
    if 11 <= n % 100 <= 14:
        return many
    r = n % 10
    if r == 1:
        return one
    if 2 <= r <= 4:
        return few
    return many


def _money_words(whole: str, frac: str | None) -> str:
    """'510','00' → '510 гривень'; '510','10' → '510 гривень 10 копійок'. Digits are kept
    (espeak reads them); we attach the correctly-declined currency word and drop a .00."""
    hr = int(whole)
    kop = int(frac.ljust(2, "0")[:2]) if frac else 0
    parts: list[str] = []
    if hr or not kop:
        parts.append(f"{hr} {_ua_plural(hr, 'гривня', 'гривні', 'гривень')}")
    if kop:
        parts.append(f"{kop} {_ua_plural(kop, 'копійка', 'копійки', 'копійок')}")
    return " ".join(parts)


_GROUP_RE = re.compile(r"(?<=\d)[   ](?=\d{3}\b)")  # "2 391" → "2391"
_DATE_DMY_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")  # 2026-06-06
_DATE_MY_RE = re.compile(r"\b(\d{4})-(\d{2})\b")  # 2026-06
_ORD_GO_RE = re.compile(r"\b(\d{1,2})-го\b")  # «до 20-го» → «до двадцятого»
_MONEY_RE = re.compile(
    r"(\d+)(?:[.,](\d{1,2}))?\s*(?:₴|грн\.?|гривень|гривні|гривня)", re.IGNORECASE
)
_DROP_ZEROS_RE = re.compile(r"(\d)[.,]0{1,2}(?!\d)")  # "510.00" → "510"
_RANGE_RE = re.compile(r"(\d)\s*[–—]\s*(\d)")  # "28–30" → "28 до 30"
_SP_DASH_RE = re.compile(r"\s[-–—]+\s")  # « Світло — 420 » → « Світло, 420 »
_DECIMAL_RE = re.compile(r"(\d+)[.,](\d+)")  # "1888.14" → "1888 кома 14"
# A dotted identifier — a login/contract number like «00.28.00.36» (≥3 groups, so a plain
# decimal «1888.14» with its single dot is left to the decimal pass). espeak would read
# every dot as «крапка» («00 крапка 28 крапка…»); voice the groups with short pauses
# instead, so it sounds like a number read aloud, not «крапка крапка крапка».
_CODE_RE = re.compile(r"\b\d+(?:\.\d+){2,}\b")
_PERCENT_RE = re.compile(r"(\d+)\s*%")
# Meter readings/usage: "3.03 м³" → spoken «… кубометр(а/и/ів)», so a voiced reading names
# its unit, not a bare number («спожито 3.03» → «спожито чого?»). Owns its decimal so
# leading zeros are voiced (see `_spoken_frac`); runs BEFORE the generic decimal pass.
_VOLUME_RE = re.compile(r"(\d+)(?:[.,](\d+))?\s*м(?:³|3)(?!\w)")
# «<місяць> 2026» → «<місяць> дві тисячі двадцять шостого року»: a period is spoken with
# the year in full (genitive ordinal + «року»), the way a person says it — not a bare
# cardinal «дві тисячі двадцять шість» with no «року» (what dead-ended «червень 2026»).
_MONTH_YEAR_RE = re.compile(r"\b(" + "|".join(_MONTHS_NOM.values()) + r")\s+(\d{4})\b")
# Latin brand/jargon terms espeak's uk voice mispronounces (it reads Latin letters with
# its own rules: «monobank» comes out «монобайк»). Give each a Ukrainian spoken form;
# matched case-insensitively, longest-first so «gigabit+» wins over «gigabit».
_SPOKEN_TERMS: dict[str, str] = {
    "monobank": "монобанк",
    "autopay": "автосписання",
    "gigabit+": "гігабіт плюс",
    "gigabit": "гігабіт",
}
_TERMS_RE = re.compile(
    "|".join(re.escape(k) for k in sorted(_SPOKEN_TERMS, key=len, reverse=True)),
    re.IGNORECASE,
)


def _date_dmy(m: re.Match[str]) -> str:
    y, mo, d = int(m[1]), int(m[2]), int(m[3])
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return m.group(0)
    return f"{_two_ord_gen(d)} {_MONTHS_GEN[mo]} {_year_ord_gen(y)} року"


def _date_my(m: re.Match[str]) -> str:
    y, mo = int(m[1]), int(m[2])
    return f"{_MONTHS_NOM[mo]} {y}" if 1 <= mo <= 12 else m.group(0)


def _month_year(m: re.Match[str]) -> str:
    return f"{m[1]} {_year_ord_gen(int(m[2]))} року"


def _spoken_frac(frac: str) -> str:
    """Fractional digits as read aloud, KEEPING leading zeros — «03» → «нуль три», not
    «три» (which espeak would voice as .3). All-zero «00» → «нуль»."""
    stripped = frac.lstrip("0")
    if not stripped:
        return "нуль"
    return "нуль " * (len(frac) - len(stripped)) + stripped


def _decimal_words(m: re.Match[str]) -> str:
    return f"{m[1]} кома {_spoken_frac(m[2])}"


def _volume_words(m: re.Match[str]) -> str:
    """'3.03 м³' → '3 кома нуль три кубометра'; '5 м³' → '5 кубометрів'. After a decimal
    the unit is genitive singular (кубометра); a whole number gets plural agreement."""
    whole, frac = m[1], m[2]
    if frac and int(frac) != 0:
        return f"{whole} кома {_spoken_frac(frac)} кубометра"
    n = int(whole)
    return f"{n} {_ua_plural(n, 'кубометр', 'кубометри', 'кубометрів')}"


def voiceify(text: str) -> str:
    """Turn a screen-oriented reply into natural spoken Ukrainian, so the butler sounds
    like a person, not a screen-reader. Drops emoji/quotes/brackets/markup; reads money as
    «510 гривень [10 копійок]» (declined), dates as «шостого червня дві тисячі двадцять
    шостого року», a period «червень 2026» with the year in full + «року», meter volumes
    «3.03 м³» as «3 кома нуль три кубометра», «20-го» as «двадцятого», decimals as «1888
    кома 14» (leading zeros voiced: «03» → «нуль 3»); a dotted code «00.28.00.36» as
    pause-separated groups (not «крапка крапка»); gives Latin brand/jargon terms a
    spoken Ukrainian form («monobank» → «монобанк», else espeak says «монобайк»); turns
    dashes into pauses and folds newlines/bullets into sentences. '' if empty.

    Note: Ukrainian lexical stress is left to espeak's own guess — espeak-ng has no
    handling for an explicit stress mark (U+0301), so we don't try to inject one."""
    if not text:
        return ""
    out = _EMOJI_RE.sub("", text)
    out = _STRIP_RE.sub(" ", out)
    out = _MARKUP_RE.sub(" ", out)
    # Fold each line into a sentence so the voice breathes between them.
    lines = [ln.strip(" -–—·•\t") for ln in out.splitlines()]
    out = ". ".join(ln for ln in lines if ln)
    # Numbers & dates → spoken forms (order matters: ungroup → dates → money → volume →
    # decimals, so the wider patterns claim their digits before the bare-decimal pass).
    out = _GROUP_RE.sub("", out)
    out = _DATE_DMY_RE.sub(_date_dmy, out)
    out = _DATE_MY_RE.sub(_date_my, out)
    out = _MONTH_YEAR_RE.sub(_month_year, out)  # «червень 2026» → «… шостого року»
    out = _ORD_GO_RE.sub(lambda m: _two_ord_gen(int(m[1])), out)
    # A dotted contract/login code → pause-separated groups (before the decimal/zero
    # passes claim its dots), so «00.28.00.36» isn't read «00 крапка 28 крапка …».
    out = _CODE_RE.sub(lambda m: m.group(0).replace(".", ", "), out)
    out = _MONEY_RE.sub(lambda m: _money_words(m[1], m[2]), out)
    out = _VOLUME_RE.sub(_volume_words, out)  # «3.03 м³» → «3 кома нуль три кубометра»
    out = _DROP_ZEROS_RE.sub(r"\1", out)
    out = _RANGE_RE.sub(r"\1 до \2", out)
    out = _SP_DASH_RE.sub(", ", out)
    out = _DECIMAL_RE.sub(_decimal_words, out)
    out = _PERCENT_RE.sub(
        lambda m: f"{m[1]} {_ua_plural(int(m[1]), 'відсоток', 'відсотки', 'відсотків')}",
        out,
    )
    for sym, word in (
        ("₴", " гривень"),
        ("№", " номер "),
        ("≈", " приблизно "),
        ("&", " і "),
    ):
        out = out.replace(sym, word)
    # Latin brand/jargon terms → an explicit Ukrainian spoken form: espeak's uk voice
    # reads a Latin word with its own rules and mangles it («monobank» → «монобайк»).
    out = _TERMS_RE.sub(lambda m: _SPOKEN_TERMS[m.group(0).lower()], out)
    # Tidy spacing/punctuation left by the substitutions.
    out = re.sub(r"\s+", " ", out).strip()
    out = re.sub(r"\s+([,.])", r"\1", out)
    out = re.sub(r"([,.])(?:\s*\1)+", r"\1", out)
    out = re.sub(r",\s*\.", ".", out).strip()
    return out


class TTSProvider(ABC):
    @abstractmethod
    async def synthesize(self, text: str) -> str | None:
        """Path to a spoken-audio file (OGG/Opus) for `text`, or None if nothing was
        produced (disabled, no model, too long, or a synth error)."""
        ...


class NullTTSProvider(TTSProvider):
    """Used when TTS is disabled (tts_provider="none") — never synthesizes."""

    async def synthesize(self, text: str) -> str | None:
        return None


class PiperTTSProvider(TTSProvider):
    """Local Piper TTS via its external binary; ffmpeg (already on the box for Whisper)
    re-encodes the WAV into the OGG/Opus that Telegram voice notes require. Returns None
    (→ the bot sends text) on any failure or when no voice model is configured."""

    _DIR = Path(tempfile.gettempdir()) / "dvoretskyi_tts"

    async def synthesize(self, text: str) -> str | None:
        s = get_settings()
        spoken = voiceify(text)
        if not spoken or not s.piper_voice:
            return None
        if len(spoken) > s.tts_max_chars:
            # Too long to voice comfortably — let the bot send it as text instead.
            return None
        try:
            return await asyncio.wait_for(
                self._run(spoken, s), timeout=s.tts_timeout_seconds
            )
        except TimeoutError:
            log.warning("tts synth timed out after %ss", s.tts_timeout_seconds)
        except Exception:
            log.exception("tts synth failed")
        return None

    async def _run(self, spoken: str, s: Settings) -> str | None:
        self._DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        wav_fd, wav_path = tempfile.mkstemp(suffix=".wav", dir=str(self._DIR))
        os.close(wav_fd)
        ogg_path = wav_path[:-4] + ".ogg"
        try:
            # 1) Piper: reply text (stdin) → WAV. The .json config is auto-loaded next to
            #    the .onnx model, so only --model is required.
            piper_args = [
                s.piper_bin,
                "--model",
                s.piper_voice,
                "--output_file",
                wav_path,
            ]
            if s.piper_length_scale:
                piper_args += ["--length_scale", s.piper_length_scale]
            if s.piper_sentence_silence:
                piper_args += ["--sentence_silence", s.piper_sentence_silence]
            espeak_dir = _espeak_data_dir(s)
            if espeak_dir:
                # Custom espeak data dir with our uk stress overrides (else espeak's own,
                # sometimes-wrong, rule stress is used). See config / build_espeak_stress.
                piper_args += ["--espeak_data", espeak_dir]
            if not await self._spawn(piper_args, stdin=spoken.encode("utf-8")):
                return None
            if not os.path.getsize(wav_path):
                return None
            # 2) ffmpeg: WAV → OGG/Opus (mono, 48 kHz — the voice-note codec).
            ok = await self._spawn(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    wav_path,
                    "-c:a",
                    "libopus",
                    "-b:a",
                    "32k",
                    "-ar",
                    "48000",
                    "-ac",
                    "1",
                    ogg_path,
                ]
            )
            if not ok or not os.path.exists(ogg_path) or not os.path.getsize(ogg_path):
                return None
            return ogg_path
        finally:
            # Keep only the OGG (the caller deletes that after sending); drop the WAV now.
            if os.path.exists(wav_path):
                os.unlink(wav_path)

    @staticmethod
    async def _spawn(args: list[str], *, stdin: bytes | None = None) -> bool:
        """Run a subprocess, feeding optional stdin. True on a clean (rc 0) exit."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _out, err = await proc.communicate(stdin)
        except (FileNotFoundError, OSError) as exc:
            log.warning("tts subprocess %r unavailable: %s", args[0], exc)
            return False
        if proc.returncode != 0:
            log.warning(
                "tts subprocess %r exited %s: %s",
                args[0],
                proc.returncode,
                err.decode("utf-8", "replace")[:300],
            )
            return False
        return True


def get_tts_provider() -> TTSProvider:
    return (
        NullTTSProvider()
        if get_settings().tts_provider.casefold() == "none"
        else PiperTTSProvider()
    )
