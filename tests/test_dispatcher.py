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
    # The persona preamble is kept and the rendered stats summary is appended, so the
    # numbers actually reach the user (not just "зараз гляну").
    assert reply.text.startswith(line)
    assert "480" in reply.text
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


async def test_promise_without_tool_triggers_one_retry(session, providers):
    # «давай по воді» → model stalls with a promise and no tool; the guard re-asks once
    # and the second answer actually calls a tool, so the user gets data not a preamble.
    llm = FakeLLMProvider(
        [
            Decision(tool=None, args={}, message="Зараз підніму показники, секунду. 🎩"),
            Decision(tool="get_unpaid", args={}, message="Ось що відкрите."),
        ]
    )
    reply = await dispatcher.handle_message("давай по воді", session, llm)
    assert reply.tool == "get_unpaid"  # it retried into a real tool
    assert len(llm.calls) == 2  # exactly one retry
    assert "СИСТЕМА" in llm.calls[1][0]  # the corrective nudge rode the second prompt


async def test_promise_retry_does_not_loop(session, providers):
    # If the model keeps promising without a tool, we retry only once and surface the
    # message — never an infinite re-ask loop.
    llm = FakeLLMProvider(
        [Decision(tool=None, args={}, message="Зараз гляну, хвилинку.")]
    )
    reply = await dispatcher.handle_message("а по газу?", session, llm)
    assert reply.tool is None
    assert reply.text == "Зараз гляну, хвилинку."
    assert len(llm.calls) == 2  # retried once, then gave up gracefully


async def test_plain_no_tool_reply_does_not_retry(session, providers):
    # A normal no-tool answer (no «зараз гляну» promise) must NOT trigger the retry.
    llm = FakeLLMProvider(
        [Decision(tool=None, args={}, message="Усе закрито — питань нема.")]
    )
    reply = await dispatcher.handle_message("як справи?", session, llm)
    assert reply.text == "Усе закрито — питань нема."
    assert len(llm.calls) == 1  # no needless second call


async def test_progress_line_sent_and_preamble_dropped(session, providers, monkeypatch):
    # With on_progress (voice), the bot first says a natural «I'm on it» line, and the
    # answer carries just the data — no «Гляну баланс» preamble doubling it up.
    from dvoretskyi.agent import tools as tools_mod

    async def fake_balance(session, **kw):
        return {"ok": True, "message": "Усе гаразд: 400 ₴ на рахунку."}

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
    progress: list[str] = []

    async def on_progress(line: str) -> None:
        progress.append(line)

    reply = await dispatcher.handle_message(
        "скільки на інтернеті?", session, llm, on_progress=on_progress
    )
    assert len(progress) == 1 and progress[0]  # one natural acknowledgment
    assert reply.text == "Усе гаразд: 400 ₴ на рахунку."  # preamble dropped
    assert "Гляну баланс." not in reply.text


async def test_progress_not_sent_for_a_plain_chat_reply(session, providers):
    # A joke / chat answer (no tool) must not trigger a progress line.
    llm = FakeLLMProvider([Decision(tool=None, args={}, message="Ось вам жарт. 🎩")])
    progress: list[str] = []

    async def on_progress(line: str) -> None:
        progress.append(line)

    reply = await dispatcher.handle_message(
        "розкажи жарт", session, llm, on_progress=on_progress
    )
    assert progress == []  # nothing to be «on», so no «I'm on it» line
    assert reply.text == "Ось вам жарт. 🎩"
