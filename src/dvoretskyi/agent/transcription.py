"""Speech-to-text for Telegram voice notes, behind a `TranscriptionProvider` abstraction.

The default `WhisperTranscriptionProvider` runs faster-whisper locally (CTranslate2), so
the audio never leaves the box — the OGG/Opus file is deleted right after, exactly like a
meter photo. Audio bytes are never logged.

On any failure the contract is an empty string — the voice handler then asks the user to
repeat (or type) rather than acting on a misheard command. Meter *values* still come only
from photos: STT misreads digits, and the spec forbids guessing a reading.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from dvoretskyi.config import get_settings

log = logging.getLogger(__name__)


class TranscriptionProvider(ABC):
    @abstractmethod
    async def transcribe(self, audio_path: str) -> str: ...


class NullTranscriptionProvider(TranscriptionProvider):
    """Used when STT is disabled (stt_provider="none") — never transcribes."""

    async def transcribe(self, audio_path: str) -> str:
        return ""


class WhisperTranscriptionProvider(TranscriptionProvider):
    """Local faster-whisper. The (heavy) model is loaded once and cached on the class —
    the first voice note pays the load cost, the rest are fast. ffmpeg (already on the
    VPS) decodes the OGG/Opus that Telegram sends."""

    _model = None  # class-level cache: one model per process, loaded lazily

    @classmethod
    def _get_model(cls):
        if cls._model is None:
            from faster_whisper import WhisperModel  # lazy: only when actually used

            s = get_settings()
            cls._model = WhisperModel(
                s.whisper_model, device="cpu", compute_type=s.whisper_compute_type
            )
        return cls._model

    async def transcribe(self, audio_path: str) -> str:
        s = get_settings()
        try:
            # Whisper is blocking + CPU-bound → off the event loop, with a hard timeout.
            return await asyncio.wait_for(
                asyncio.to_thread(self._run, audio_path),
                timeout=s.stt_timeout_seconds,
            )
        except TimeoutError:
            log.warning("transcription timed out after %ss", s.stt_timeout_seconds)
        except Exception:
            log.exception("transcription failed")
        return ""

    def _run(self, audio_path: str) -> str:
        model = self._get_model()
        lang = get_settings().whisper_language or None
        segments, _info = model.transcribe(audio_path, language=lang, vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()


def get_transcription_provider() -> TranscriptionProvider:
    return (
        NullTranscriptionProvider()
        if get_settings().stt_provider.casefold() == "none"
        else WhisperTranscriptionProvider()
    )
