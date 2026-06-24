from __future__ import annotations

from dvoretskyi.agent import provider as provider_mod
from dvoretskyi.agent.provider import ClaudeCodeProvider, parse_decision


def test_parse_decision_strips_fences_and_null_tool():
    d = parse_decision('```json\n{"tool": "null", "args": {}, "message": "привіт"}\n```')
    assert d is not None
    assert d.tool is None  # "null"/"" → no tool
    assert d.message == "привіт"


def test_parse_decision_rejects_non_json():
    assert parse_decision("вибач, не json") is None


async def test_decide_retries_after_a_transient_failure(monkeypatch):
    """A single None (e.g. the observed 60s timeout) must NOT dead-end — the second
    attempt rides out the transient and returns the real decision."""
    p = ClaudeCodeProvider()
    results: list[str | None] = [
        None,  # first call: simulate a timeout
        '{"tool": "get_unpaid", "args": {}, "message": "ось що відкрите"}',
    ]

    async def fake_invoke(prompt: str) -> str | None:
        return results.pop(0)

    monkeypatch.setattr(p, "_invoke", fake_invoke)
    decision = await p.decide("що сплатити?", {})
    assert decision.tool == "get_unpaid"
    assert decision.message == "ось що відкрите"
    assert results == []  # both attempts were consumed (it retried)


class _FakeProc:
    """Minimal stand-in for an asyncio subprocess that returns a valid decision blob."""

    returncode = 0

    async def communicate(self, _stdin: bytes) -> tuple[bytes, bytes]:
        return (b'{"result": "{\\"tool\\": null, \\"message\\": \\"ok\\"}"}', b"")


async def test_invoke_pins_a_fast_model(monkeypatch):
    """The decision turn passes --model so every reply runs on a fast model, not the
    CLI's heavy default — the core latency win."""
    captured: dict = {}

    async def fake_exec(*args, **_kwargs):
        captured["argv"] = args
        return _FakeProc()

    monkeypatch.setattr(provider_mod.asyncio, "create_subprocess_exec", fake_exec)
    p = ClaudeCodeProvider()
    p.model = "claude-sonnet-4-6"
    out = await p._invoke("prompt")
    assert out == '{"tool": null, "message": "ok"}'  # unwrapped from `.result`
    argv = captured["argv"]
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"


async def test_invoke_omits_model_when_unset(monkeypatch):
    """Empty claude_model → no --model flag, falling back to the CLI default."""
    captured: dict = {}

    async def fake_exec(*args, **_kwargs):
        captured["argv"] = args
        return _FakeProc()

    monkeypatch.setattr(provider_mod.asyncio, "create_subprocess_exec", fake_exec)
    p = ClaudeCodeProvider()
    p.model = ""
    await p._invoke("prompt")
    assert "--model" not in captured["argv"]


async def test_decide_falls_back_after_two_failures(monkeypatch):
    p = ClaudeCodeProvider()

    async def always_none(prompt: str) -> str | None:
        return None

    monkeypatch.setattr(p, "_invoke", always_none)
    decision = await p.decide("привіт", {})
    assert decision.tool is None
    assert decision.message == provider_mod._SAFE_FALLBACK
