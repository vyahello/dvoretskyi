from __future__ import annotations

from decimal import Decimal

from dvoretskyi import clock
from dvoretskyi.agent import dispatcher
from dvoretskyi.agent.provider import Decision
from dvoretskyi.db.models import Payment, PaymentSource
from tests.conftest import FakeLLMProvider


async def test_no_tool_passes_message_through(session, providers):
    llm = FakeLLMProvider(
        [Decision(tool=None, args={}, message="Усе спокійно, мій пане.")]
    )
    reply = await dispatcher.handle_message("як справи?", session, llm)
    assert reply.text == "Усе спокійно, мій пане."
    assert reply.tool is None


async def test_routes_to_get_unpaid_and_keeps_persona_text(session, providers):
    persona_line = "Відкрите: газ і вода. Решта спить."
    llm = FakeLLMProvider([Decision(tool="get_unpaid", args={}, message=persona_line)])
    reply = await dispatcher.handle_message("що платити?", session, llm)
    assert reply.tool == "get_unpaid"
    assert reply.text == persona_line  # persona text passed through untouched
    assert reply.tool_result is not None and "open" in reply.tool_result


async def test_tool_result_message_is_appended(session, providers, monkeypatch):
    # A tool that computes data the LLM didn't have (e.g. a scraped balance) returns it
    # as result["message"]; the dispatcher must surface it, not just the persona preamble.
    from dvoretskyi.agent import tools as tools_mod

    async def fake_balance(session, **kw):
        return {"ok": True, "message": "Баланс 400 ₴ — платити не треба."}

    monkeypatch.setitem(tools_mod.TOOLS, "get_provider_balance", fake_balance)
    llm = FakeLLMProvider(
        [
            Decision(
                tool="get_provider_balance",
                args={"provider_name": "Інтернет (Gigabit+)"},
                message="Гляну баланс.",
            )
        ]
    )
    reply = await dispatcher.handle_message("скільки на інтернеті?", session, llm)
    assert "Гляну баланс." in reply.text
    assert "Баланс 400 ₴ — платити не треба." in reply.text  # tool result surfaced


async def test_routes_to_get_stats_attaches_chart(session, providers):
    gas = providers["Газ (постачання)"]
    session.add(
        Payment(
            provider_id=gas.id,
            amount_uah=Decimal("480.00"),
            paid_at=clock.now(),
            source=PaymentSource.mono_webhook,
            raw_description="",
            mono_tx_id="d1",
        )
    )
    await session.commit()

    line = "Цього місяця 480 ₴. 📊"
    llm = FakeLLMProvider(
        [Decision(tool="get_stats", args={"period": "all"}, message=line)]
    )
    reply = await dispatcher.handle_message("статистика", session, llm)
    assert reply.text == line
    assert reply.chart_path is not None

    import os

    os.unlink(reply.chart_path)


async def test_tool_error_surfaced_without_crashing(session, providers):
    line = "Записую."
    llm = FakeLLMProvider(
        [
            Decision(
                tool="log_payment_manual",
                args={"provider_name": "Нема", "amount": "10"},
                message=line,
            )
        ]
    )
    reply = await dispatcher.handle_message("запиши", session, llm)
    assert reply.error is not None
    assert line in reply.text  # persona line preserved, warning appended


async def test_unknown_tool_ignored(session, providers):
    llm = FakeLLMProvider([Decision(tool="teleport", args={}, message="Гм.")])
    reply = await dispatcher.handle_message("хочу телепорт", session, llm)
    assert reply.text == "Гм."
    assert reply.error is not None
