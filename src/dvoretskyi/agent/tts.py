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

# Emoji / pictographs / arrows / symbols that a voice would read as "галочка", "стрілка",
# … — strip them. ₴ and the like are handled by _REPLACEMENTS below (read as words).
_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff"  # pictographs, transport, supplemental, flags (1F1E6–1F1FF)
    "←-⇿"  # arrows (e.g. ↪ «Це інше житло»)
    "⌀-⏿"  # misc technical (⏳ ⏰)
    "☀-➿"  # misc symbols + dingbats (✅ ✂)
    "⬀-⯿"  # extra arrows/stars
    "️‍]"  # variation selector + zero-width joiner
)
# Markdown-ish punctuation the persona avoids but might still slip in.
_MARKUP_RE = re.compile(r"[*_`#>|]")

# Symbols a screen reply uses freely → spoken words.
_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("₴", " гривень"),
    ("грн", " гривень"),
    ("%", " відсотків"),
    ("№", " номер "),
    ("≈", " приблизно "),
    ("&", " і "),
)


def voiceify(text: str) -> str:
    """Turn a screen-oriented reply into clean spoken Ukrainian: drop emoji and markup,
    expand symbols (₴ → «гривень»), and fold newlines/bullets into sentences so the voice
    pauses naturally. Returns '' for empty input."""
    if not text:
        return ""
    out = _EMOJI_RE.sub("", text)
    for sym, word in _REPLACEMENTS:
        out = out.replace(sym, word)
    out = _MARKUP_RE.sub(" ", out)
    # Each line becomes a sentence so the synth breathes between them.
    lines = [ln.strip(" -–—·•\t") for ln in out.splitlines()]
    out = ". ".join(ln for ln in lines if ln)
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"\.\s*\.", ".", out)  # collapse periods doubled by the join
    return out.strip()


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
