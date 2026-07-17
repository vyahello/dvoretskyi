"""Pulling a JSON object out of chatty model output.

Shared by the decision turn (`agent/provider.py`) and the vision turn
(`agent/vision.py`). Both ask the model for "ONLY JSON" and both sometimes get prose or
a ```json fence around it anyway — so both need the same forgiving extraction, and there
is no reason for the text path to be less robust than the image path.
"""

from __future__ import annotations

import json
import re

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _top_level_objects(text: str) -> list[str]:
    """Every balanced {...} run that is NOT nested inside another one, in order.

    Top-level is the important word. Scanning for "the last '{'" finds an INNER object
    whenever the JSON has nested keys — for a decision like
    `{"tool": "get_stats", "args": {"period": "2026-05"}}` it returns `{"period": …}`,
    which parses fine, has no "tool", and silently becomes "no tool at all". (The vision
    payload is flat, which is why that path never noticed.) So: find a balanced run, then
    skip past its end before looking for the next one.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        escaped = False
        for j in range(i, n):
            ch = text[j]
            # Braces inside a JSON STRING are text, not structure. The butler's own
            # `message` is free-form Ukrainian and may well contain one — counting it
            # would unbalance the scan and drop the whole decision on the floor.
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    out.append(text[i : j + 1])
                    i = j + 1
                    break
        else:  # unbalanced from here to the end — nothing more to find
            break
        if depth != 0:
            break
    return out


def extract_json_object(text: str) -> dict | None:
    """Best-effort: return the JSON object in `text`, else None.

    Tries, in order: the whole string, any fenced block, then top-level balanced {...}
    runs — **last first**, because the answer comes last: «Наприклад: {…}. Відповідь:
    {…}» must resolve to the answer, not the example. This pass is what rescues a single
    line of preamble, which would otherwise cost a whole extra LLM round-trip and then a
    "my brain broke" apology.
    """
    if not text:
        return None
    # Last-first throughout: the answer comes last, so an EXAMPLE — fenced or not — must
    # never outrank it. (Fences were being tried first-first, which let «Ось приклад:
    # ```json {…}``` А ось відповідь: {…}» resolve to the example.)
    candidates: list[str] = [text.strip()]
    candidates += reversed(_FENCE_RE.findall(text))
    candidates += reversed(_top_level_objects(text))
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None
