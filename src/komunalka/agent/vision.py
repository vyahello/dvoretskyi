"""Vision OCR for meter photos, behind a `VisionProvider` abstraction.

The default `ClaudeCodeVisionProvider` drives the first-party Claude Code CLI with the
`Read` tool enabled so it can view the image at a path. Same env hardening as the text
provider: `ANTHROPIC_API_KEY` is stripped so the Max subscription is used (spec §8).

On any failure the contract is `value=None` — the pipeline then asks the user to retype
the reading rather than guessing. Image bytes are never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from komunalka.config import get_settings

log = logging.getLogger(__name__)

_OCR_PROMPT = (
    "На зображенні за шляхом {path} — фото комунального лічильника (газ або вода). "
    "Прочитай ПОВНЕ показання зліва направо, ВКЛЮЧНО з дробовою частиною: "
    "червоні барабани/цифри — це і є знаки після коми, їх треба читати, а не відкидати. "
    "Поверни значення як рядок із крапкою-роздільником, без округлень і без одиниць "
    '(напр. "103.999", "4827.05"). Якщо дробових розрядів нема — поверни ціле. '
    "Поверни ЛИШЕ JSON без жодного тексту поза ним: "
    '{{"value": "<повне показання|null>", "raw": "<цифри як бачиш>", '
    '"note": "<короткий коментар>"}}'
)


@dataclass
class MeterRead:
    value: Decimal | None
    raw: str
    note: str


class VisionProvider(ABC):
    @abstractmethod
    async def read_meter(self, image_path: str) -> MeterRead: ...


def downscale(image_path: str, max_long_side: int) -> tuple[str, bool]:
    """Downscale to `max_long_side` on the long edge → temp JPEG. Returns
    (path, is_temp). On any Pillow error, falls back to the original path."""
    try:
        from PIL import Image

        with Image.open(image_path) as img:
            if max(img.size) <= max_long_side:
                return image_path, False
            rgb = img.convert("RGB")
            rgb.thumbnail((max_long_side, max_long_side))
            tmp = tempfile.NamedTemporaryFile(
                prefix="komunalka_ocr_", suffix=".jpg", delete=False
            )
            rgb.save(tmp.name, format="JPEG", quality=85)
            return tmp.name, True
    except Exception as exc:  # noqa: BLE001 — never block OCR on a resize failure
        log.warning("downscale failed (%s); using original", exc)
        return image_path, False


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json_object(text: str) -> dict | None:
    """Pull a JSON object out of possibly-chatty model output.

    Vision turns often wrap the JSON in prose and/or a ```json fence despite the
    'only JSON' instruction. Try, in order: whole string, a fenced block, then the
    last balanced {...} run.
    """
    candidates: list[str] = [text.strip()]
    candidates += _FENCE_RE.findall(text)
    # Last balanced object: scan from each '{' and track brace depth.
    starts = [m.start() for m in re.finditer(r"\{", text)]
    for start in reversed(starts):
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break
        if depth == 0:
            break
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _parse_meter_read(raw: str) -> MeterRead | None:
    """Parse a model text blob into a MeterRead; None if no JSON object found."""
    data = _extract_json_object(raw)
    if data is None:
        return None
    value: Decimal | None
    rawval = data.get("value")
    if rawval in (None, "", "null"):
        value = None
    else:
        # Keep full precision — provider-specific rounding happens in the pipeline,
        # where Provider.meter_decimals is known. Accept a comma decimal separator.
        try:
            value = Decimal(str(rawval).strip().replace(",", "."))
        except (InvalidOperation, ValueError):
            value = None
    return MeterRead(
        value=value,
        raw=str(data.get("raw", "")),
        note=str(data.get("note", "")),
    )


class ClaudeCodeVisionProvider(VisionProvider):
    """OCR via `claude -p --allowed-tools "Read"` (CLI can open the image path)."""

    def __init__(self) -> None:
        settings = get_settings()
        self.bin = settings.claude_bin
        self.timeout = settings.claude_vision_timeout_seconds
        self.max_long_side = settings.ocr_max_long_side

    @staticmethod
    def _child_env() -> dict[str, str]:
        # MANDATORY (spec §8): strip ANTHROPIC_API_KEY so the Max subscription is used.
        return {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

    async def _invoke(self, prompt: str) -> str | None:
        args = [
            self.bin,
            "-p",
            prompt,
            "--allowed-tools",
            "Read",
            "--output-format",
            "json",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._child_env(),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except TimeoutError:
            log.warning("claude vision timed out after %ss", self.timeout)
            return None
        except (FileNotFoundError, OSError) as exc:
            log.error("claude binary unavailable: %s", exc)
            return None

        if proc.returncode != 0:
            log.warning(
                "claude vision exited %s: %s",
                proc.returncode,
                stderr.decode("utf-8", "replace")[:500],
            )
            return None

        try:
            outer = json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError:
            return stdout.decode("utf-8", "replace")
        return outer.get("result") if isinstance(outer, dict) else None

    async def read_meter(self, image_path: str) -> MeterRead:
        path, is_temp = downscale(image_path, self.max_long_side)
        prompt = _OCR_PROMPT.format(path=path)
        try:
            for attempt in (1, 2):
                raw = await self._invoke(prompt)
                if raw is None:
                    break
                parsed = _parse_meter_read(raw)
                if parsed is not None:
                    return parsed
                log.info("claude vision unparseable JSON (attempt %s)", attempt)
        finally:
            if is_temp and path != image_path and os.path.exists(path):
                os.unlink(path)
        return MeterRead(value=None, raw="", note="OCR не вдалося — перепиши вручну.")


def get_vision_provider() -> VisionProvider:
    # Single implementation for now; kept behind the ABC for a future API swap.
    return ClaudeCodeVisionProvider()
