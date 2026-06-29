"""Vision OCR for meter photos, behind a `VisionProvider` abstraction.

The default `ClaudeCodeVisionProvider` drives the first-party Claude Code CLI with the
`Read` tool enabled so it can view the image at a path.

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

from dvoretskyi.config import get_settings

log = logging.getLogger(__name__)

_OCR_PROMPT = (
    "На зображенні за шляхом {path} може бути фото комунального лічильника (газ або "
    "вода), а може — щось зовсім інше. Визнач:\n"
    '1) kind: "water" — якщо це лічильник і він ТЕМНИЙ (чорний/темний корпус чи табло); '
    '"gas" — якщо це лічильник і він СВІТЛИЙ; "other" — якщо на фото не лічильник.\n'
    "2) Якщо це лічильник — прочитай ПОВНЕ показання зліва направо. Спершу полічи, "
    "СКІЛЬКИ всього цифрових барабанів (коліщат) у цілій частині, і прочитай КОЖЕН — "
    "зокрема КРАЙНІ ЛІВІ цифри та нулі попереду (не відкидай провідні цифри: «108» — це "
    "не «14» і не «8»). Дивись на кожен барабан ОКРЕМО і бери ту цифру, що по центру "
    "віконця (на лінії); якщо барабан між двома цифрами — бери МЕНШУ (нижню). Будь "
    "особливо уважним до цифр, які легко сплутати: 1↔2↔7, 6↔8, 0↔8, 3↔8, 5↔6, 9↔8 — "
    "пильно звір кожну. Потім додай дробову частину (червоні барабани/цифри — це знаки "
    "після коми, їх теж читай). Поверни як рядок із крапкою, без округлень і одиниць "
    '(напр. "103.999", "4827.05"). У полі "raw" впиши всі цифри так, як їх бачиш, зліва '
    "направо, нічого не пропускаючи.\n"
    '3) Якщо kind="other" — value=null, а в comment напиши КОРОТКИЙ доброзичливий жарт '
    "українською про те, що на фото.\n"
    "Поверни ЛИШЕ JSON без тексту поза ним: "
    '{{"kind": "water|gas|other", "value": "<показання|null>", '
    '"raw": "<цифри як бачиш>", "comment": "<жарт, якщо other>"}}'
)

# Appended when we know the previous filed value: an anchor that lets the model resolve
# an ambiguous wheel (a rounded 0 misread as 4 → 148 instead of 108) the way a human does
# — by knowing roughly where the meter stood. Must NOT override a clearly-different digit.
_HINT_TMPL = (
    "\n\nПІДКАЗКА: попередній поданий показник ЦЬОГО лічильника — близько {prev}. "
    "Лічильник лише РОСТЕ і зазвичай на невелику величину, тож ціла частина нового "
    "показання майже напевно така сама або трохи більша за цілу частину {prev}. Якщо "
    "твоє зчитування цілої частини дуже інше за {prev} (на десятки/сотні) — ти "
    "майже напевно сплутав цифру на барабані (округлий 0 ↔ 4, 1 ↔ 7, 6 ↔ 8 тощо): "
    "перечитай сумнівні барабани ще раз. Але НЕ підганяй штучно — якщо цифра ЧІТКО інша, "
    "довірся зображенню."
)


@dataclass
class MeterRead:
    value: Decimal | None
    raw: str
    note: str
    kind: str = ""  # "water" | "gas" | "other" (dark meter → water, light → gas)
    comment: str = ""  # witty remark when kind == "other"
    confident: bool = True  # False → independent OCR reads disagreed; treat as uncertain
    alt_value: Decimal | None = None  # the differing read, when confident is False


class VisionProvider(ABC):
    @abstractmethod
    async def read_meter(
        self, image_path: str, hint: Decimal | None = None
    ) -> MeterRead: ...


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
                prefix="dvoretskyi_ocr_", suffix=".jpg", delete=False
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
        kind=str(data.get("kind", "")).strip().lower(),
        comment=str(data.get("comment", "")),
    )


def _reconcile(reads: list[MeterRead]) -> MeterRead:
    """Combine independent OCR reads of one photo into a single verdict.

    Digit confusion is intermittent, so we trust a value only when ≥2 reads AGREE on it.
    A disagreement keeps the first read's value but marks it not-confident (and records
    the differing one in `alt_value`) — the pipeline then asks the user to confirm rather
    than file a possibly-misread number the delta check can't catch (e.g. 108→148, a
    plausible +40)."""
    if not reads:
        return MeterRead(value=None, raw="", note="OCR не вдалося — перепиши вручну.")
    first = reads[0]
    values = [r.value for r in reads if r.value is not None]
    if len(values) < 2:
        # Only one read produced a number (or it's "other"/None) — nothing to cross-check.
        return first
    if all(v == values[0] for v in values):
        return first  # unanimous → confident
    alt = next((v for v in values if v != first.value), None)
    return MeterRead(
        value=first.value,
        raw=first.raw,
        note=first.note,
        kind=first.kind,
        comment=first.comment,
        confident=False,
        alt_value=alt,
    )


class ClaudeCodeVisionProvider(VisionProvider):
    """OCR via `claude -p --allowed-tools "Read"` (CLI can open the image path)."""

    def __init__(self) -> None:
        settings = get_settings()
        self.bin = settings.claude_bin
        self.timeout = settings.claude_vision_timeout_seconds
        self.max_long_side = settings.ocr_max_long_side
        self.read_attempts = max(1, settings.ocr_read_attempts)

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

    async def read_meter(self, image_path: str, hint: Decimal | None = None) -> MeterRead:
        path, is_temp = downscale(image_path, self.max_long_side)
        prompt = _OCR_PROMPT.format(path=path)
        if hint is not None:
            prompt += _HINT_TMPL.format(prev=hint)
        try:
            # Read the photo several times in parallel (same wall-clock as one read) and
            # only trust a value the independent reads agree on — intermittent digit
            # misreads then surface as a disagreement instead of a silently-filed number.
            raws = await asyncio.gather(
                *(self._invoke(prompt) for _ in range(self.read_attempts))
            )
            reads = [
                parsed
                for raw in raws
                if raw is not None and (parsed := _parse_meter_read(raw)) is not None
            ]
            if reads:
                return _reconcile(reads)
            log.info(
                "claude vision: no parseable read in %s attempts", self.read_attempts
            )
        finally:
            if is_temp and path != image_path and os.path.exists(path):
                os.unlink(path)
        return MeterRead(value=None, raw="", note="OCR не вдалося — перепиши вручну.")


def get_vision_provider() -> VisionProvider:
    # Single implementation for now; kept behind the ABC for a future API swap.
    return ClaudeCodeVisionProvider()
