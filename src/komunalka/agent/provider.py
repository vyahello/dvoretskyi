"""LLMProvider abstraction + a headless Claude Code implementation.

The LLM is treated as a stateless endpoint: persona in, structured JSON out
(`{tool, args, message}`). Tool execution happens in Python (tools.py), not via
Claude Code's agentic tooling — hence `--allowed-tools ""`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from komunalka.agent.persona import BUTLER_SYSTEM_PROMPT, TOOL_CATALOG
from komunalka.config import get_settings

log = logging.getLogger(__name__)

_SAFE_FALLBACK = "Щось пішло не так із моїм мисленнєвим апаратом. Спробуй ще раз за мить."


@dataclass
class Decision:
    tool: str | None
    args: dict = field(default_factory=dict)
    message: str = ""


class LLMProvider(ABC):
    @abstractmethod
    async def decide(self, user_text: str, context: dict) -> Decision: ...


def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` fences a model might wrap JSON in."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def parse_decision(raw: str) -> Decision | None:
    """Parse a model text blob into a Decision; None if it isn't valid JSON."""
    candidate = _strip_fences(raw)
    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    tool = data.get("tool")
    if tool in ("", "null"):
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

    def _build_user_prompt(self, user_text: str, context: dict) -> str:
        ctx = json.dumps(context, ensure_ascii=False, default=str, indent=2)
        return (
            f"{TOOL_CATALOG}\n"
            f"КОНТЕКСТ (поточний стан, лише для довідки):\n{ctx}\n\n"
            f"ПОВІДОМЛЕННЯ КОРИСТУВАЧА:\n{user_text}\n\n"
            'Поверни ЛИШЕ JSON виду {"tool": ..., "args": {...}, "message": ...}. '
            "Жодного тексту поза JSON."
        )

    @staticmethod
    def _child_env() -> dict[str, str]:
        # MANDATORY: strip ANTHROPIC_API_KEY so Claude Code uses the Max subscription
        # and never silently bills the API (spec §8).
        return {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

    async def _invoke(self, prompt: str) -> str | None:
        """Run claude once; return the model's text (.result) or None on failure."""
        args = [
            self.bin,
            "-p",
            "--output-format",
            "json",
            "--allowed-tools",
            "",
            "--append-system-prompt",
            BUTLER_SYSTEM_PROMPT,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._child_env(),
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
                break
            decision = parse_decision(raw)
            if decision is not None:
                return decision
            log.info("claude returned unparseable JSON (attempt %s)", attempt)

        return Decision(tool=None, args={}, message=_SAFE_FALLBACK)


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
