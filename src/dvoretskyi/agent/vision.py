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
import tempfile
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation

from dvoretskyi.agent.jsonx import extract_json_object
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

# Appended when we know the previous filed value(s): an anchor that lets the model resolve
# an ambiguous wheel (a rounded 0 misread as 4 → 148 instead of 108) the way a human does
# — by knowing roughly where the meter stood. Must NOT override a clearly-different digit.
# A photo's kind is decided in the same pass, so we pass anchors for BOTH possible kinds.
_KIND_WORD = {"water": "вода", "gas": "газ"}


def _build_hint(hints: dict[str, Decimal]) -> str:
    parts = [
        f"якщо це {_KIND_WORD.get(k, k)} — попередній показник ≈ {v}"
        for k, v in hints.items()
    ]
    return (
        "\n\nПІДКАЗКА про попередній показник цього лічильника: "
        + "; ".join(parts)
        + ". Лічильник лише РОСТЕ і зазвичай на невелику величину, тож ціла частина "
        "нового показання майже напевно така сама або трохи більша. Якщо твоє зчитування "
        "цілої частини дуже інше — ти, найімовірніше, сплутав цифру (округлий "
        "0 ↔ 4, 1 ↔ 7, 6 ↔ 8 тощо): перечитай сумнівні барабани. Але НЕ підганяй — якщо "
        "цифра ЧІТКО інша, довірся зображенню."
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
        self, image_path: str, hints: dict[str, Decimal] | None = None
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


# Kept as a module-level alias: the extraction now lives in `agent/jsonx.py` so the
# decision turn gets the same robustness, and tests/imports of this name still work.
_extract_json_object = extract_json_object


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


def _reconcile(reads: list[MeterRead], expected: int | None = None) -> MeterRead:
    """Combine independent OCR reads of one photo into a single verdict.

    Digit confusion is intermittent, so we trust a value only when the reads AGREE on it.
    A disagreement keeps the winning value but marks it not-confident (and records the
    differing one in `alt_value`) — the pipeline then asks the user to confirm rather
    than file a possibly-misread number the delta check can't catch (e.g. 108→148, a
    plausible +40).

    The verdict is picked from the reads that actually produced a NUMBER, by majority —
    never from `reads[0]` just because it came back first. Which of the parallel CLI
    calls misfires is random, so anchoring on slot 0 threw away a perfectly good
    consensus whenever the flaky attempt happened to land there: a real meter photo read
    correctly twice could still be answered with «на фото не лічильник» + a joke (the
    null read's `kind="other"` and `comment` won), storing nothing.

    `expected` is how many reads were ATTEMPTED (defaults to how many came back). It's
    what tells a genuine single-read config apart from a 2-read attempt where one read
    died — the survivor of the latter was never cross-checked and must not pass as
    confident.
    """
    if not reads:
        return MeterRead(value=None, raw="", note="OCR не вдалося — перепиши вручну.")
    expected = len(reads) if expected is None else expected
    values = [r.value for r in reads if r.value is not None]
    if not values:
        # No read produced a number — a genuine OCR failure, or really not a meter.
        return reads[0]

    tally = Counter(values)
    winner, votes = tally.most_common(1)[0]
    # Take raw/kind/comment from a read that actually produced the winning number, so the
    # verdict is internally consistent (a null read's "other"/joke can't ride along).
    best = next(r for r in reads if r.value == winner)

    # Confident requires a CROSS-CHECK. With `ocr_read_attempts` > 1, one attempt coming
    # back without a number (a timeout, a non-zero exit) leaves the survivor unverified —
    # exactly the case the consensus exists for — so it must not inherit `confident=True`
    # by default. With attempts=1 there was never a cross-check to lose, so that config
    # keeps its old single-read behaviour instead of flagging every reading.
    if len(values) < 2:
        return best if expected < 2 else replace(best, confident=False)
    if len(tally) == 1:
        return best  # unanimous
    alt = next(v for v in values if v != winner)
    return MeterRead(
        value=winner,
        raw=best.raw,
        note=best.note,
        kind=best.kind,
        comment=best.comment,
        confident=votes > 1 and votes > len(values) - votes,
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

    async def read_meter(
        self, image_path: str, hints: dict[str, Decimal] | None = None
    ) -> MeterRead:
        path, is_temp = downscale(image_path, self.max_long_side)
        prompt = _OCR_PROMPT.format(path=path)
        if hints:
            prompt += _build_hint(hints)
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
                return _reconcile(reads, expected=self.read_attempts)
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
