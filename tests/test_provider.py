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


async def test_decide_falls_back_after_two_failures(monkeypatch):
    p = ClaudeCodeProvider()

    async def always_none(prompt: str) -> str | None:
        return None

    monkeypatch.setattr(p, "_invoke", always_none)
    decision = await p.decide("привіт", {})
    assert decision.tool is None
    assert decision.message == provider_mod._SAFE_FALLBACK
