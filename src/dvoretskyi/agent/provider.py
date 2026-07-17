"""LLMProvider abstraction + a headless Claude Code implementation.

The LLM is treated as a stateless endpoint: persona in, structured JSON out
(`{tool, args, message}`). Tool execution happens in Python (tools.py), not via
Claude Code's agentic tooling — hence `--allowed-tools ""`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from dvoretskyi.agent.jsonx import extract_json_object
from dvoretskyi.agent.persona import BUTLER_SYSTEM_PROMPT, TOOL_CATALOG
from dvoretskyi.config import get_settings

log = logging.getLogger(__name__)

SAFE_FALLBACK = "Щось пішло не так із моїм мисленнєвим апаратом. Спробуй ще раз за мить."


@dataclass
class Decision:
    tool: str | None
    args: dict = field(default_factory=dict)
    message: str = ""


class LLMProvider(ABC):
    @abstractmethod
    async def decide(self, user_text: str, context: dict) -> Decision: ...


def parse_decision(raw: str) -> Decision | None:
    """Parse a model text blob into a Decision; None if no JSON object is in there.

    Uses the same forgiving extraction as the vision turn (`agent/jsonx`): one line of
    preamble («Ось відповідь:») used to fail the parse outright, which cost a second
    full `claude -p` round-trip and then a «мій мисленнєвий апарат зламався» apology —
    for output that was perfectly good JSON with a sentence in front of it.
    """
    data = extract_json_object(raw)
    if data is None:
        return None
    tool = data.get("tool")
    # Guard the TYPE, not just the value: `{"tool": ["get_stats"]}` reached
    # `TOOLS.get(decision.tool)` and raised «unhashable type: 'list'».
    if not isinstance(tool, str) or tool in ("", "null"):
        tool = None
    args = data.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    return Decision(tool=tool, args=args, message=str(data.get("message", "")))


class ClaudeCodeProvider(LLMProvider):
    """Drives the first-party Claude Code CLI in headless print mode."""

    def __init__(self) -> None:
        settings = get_settings()
        self.bin = settings.claude_bin
        self.timeout = settings.claude_timeout_seconds
        self.model = settings.claude_model

    def _build_user_prompt(self, user_text: str, context: dict) -> str:
        # Pull dialogue out of the context blob so it reads as a transcript, not buried
        # JSON — short replies («давай») hinge on it.
        ctx = {k: v for k, v in context.items() if k != "recent_dialogue"}
        ctx_json = json.dumps(ctx, ensure_ascii=False, default=str, indent=2)
        dialogue = ""
        turns = context.get("recent_dialogue") or []
        if turns:
            lines = "\n".join(
                f"{'Ти' if t.get('role') == 'assistant' else 'Користувач'}: "
                f"{t.get('text', '')}"
                for t in turns
            )
            dialogue = f"ОСТАННІ РЕПЛІКИ РОЗМОВИ (зважай на них):\n{lines}\n\n"
        return (
            f"{TOOL_CATALOG}\n"
            f"КОНТЕКСТ (поточний стан, лише для довідки):\n{ctx_json}\n\n"
            f"{dialogue}"
            f"ПОВІДОМЛЕННЯ КОРИСТУВАЧА:\n{user_text}\n\n"
            'Поверни ЛИШЕ JSON виду {"tool": ..., "args": {...}, "message": ...}. '
            "Жодного тексту поза JSON."
        )

    async def _invoke(self, prompt: str) -> str | None:
        """Run claude once; return the model's text (.result) or None on failure."""
        args = [self.bin, "-p", "--output-format", "json", "--allowed-tools", ""]
        if self.model:
            # Pin a fast model for the decision turn. Picking one tool and writing a
            # single Ukrainian line doesn't need a heavyweight default — Sonnet shaves
            # seconds off every reply. Empty `claude_model` → omit, use the CLI default.
            args += ["--model", self.model]
        args += ["--append-system-prompt", BUTLER_SYSTEM_PROMPT]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")), timeout=self.timeout
            )
        except TimeoutError:
            log.warning("claude invocation timed out after %ss", self.timeout)
            return None
        except (FileNotFoundError, OSError) as exc:
            log.error("claude binary unavailable: %s", exc)
            return None

        if proc.returncode != 0:
            log.warning(
                "claude exited %s: %s",
                proc.returncode,
                stderr.decode("utf-8", "replace")[:500],
            )
            return None

        try:
            outer = json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError:
            # Some configs print the bare text; treat stdout itself as the result.
            return stdout.decode("utf-8", "replace")
        # `--output-format json` wraps the model text in `.result`.
        return outer.get("result") if isinstance(outer, dict) else None

    async def decide(self, user_text: str, context: dict) -> Decision:
        prompt = self._build_user_prompt(user_text, context)

        for attempt in (1, 2):
            raw = await self._invoke(prompt)
            if raw is None:
                # Transient failure (a 60s timeout, a non-zero exit, a slow/rate-limited
                # call). Don't dead-end the user on the first miss — retry once; a second
                # attempt usually rides it out. (Was `break`, which fell straight to the
                # fallback and made an occasional slow turn look like a hard error.)
                log.info("claude call returned nothing (attempt %s) — retrying", attempt)
                continue
            decision = parse_decision(raw)
            if decision is not None:
                return decision
            log.info("claude returned unparseable JSON (attempt %s)", attempt)

        return Decision(tool=None, args={}, message=SAFE_FALLBACK)


class AnthropicAPIProvider(LLMProvider):
    """Drop-in Anthropic API alternative. Implemented in a later phase."""

    async def decide(self, user_text: str, context: dict) -> Decision:
        raise NotImplementedError(
            "AnthropicAPIProvider is a Phase-2 swap; use llm_provider=claude_code."
        )


def get_provider() -> LLMProvider:
    name = get_settings().llm_provider
    if name == "claude_code":
        return ClaudeCodeProvider()
    if name == "anthropic_api":
        return AnthropicAPIProvider()
    raise ValueError(f"Unknown llm_provider: {name!r}")
