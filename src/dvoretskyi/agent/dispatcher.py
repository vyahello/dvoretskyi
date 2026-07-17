"""Free-text → LLM decision → deterministic tool routing → the butler's reply.

Tool routing is deterministic (Python decides whether/which TOOL runs based on the
LLM's structured choice). The persona governs only `message`; we never let the model
fabricate tool *results*.
"""

from __future__ import annotations

import inspect
import logging
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dvoretskyi import clock
from dvoretskyi.agent import tools as tools_mod
from dvoretskyi.agent.provider import SAFE_FALLBACK, Decision, LLMProvider
from dvoretskyi.agent.tools import ToolError
from dvoretskyi.config import get_settings
from dvoretskyi.db.models import MeterReading, MeterStatus, Payment, Provider

log = logging.getLogger(__name__)

# A reply that *promises* to fetch something («зараз гляну», «підніму показники»,
# «секунду») but carries no tool is a dead-end — the bot can't call back, so the user
# is left with a preamble and no data. When we see this shape we re-ask the model once
# (below), telling it to actually call the tool or answer in full without promises.
# Future-tense FETCH verbs only. «пораху», «за мить», «секунд», «момент», «хвилин» were
# dropped: they appear in perfectly good non-promise copy (the persona's own «розкажи як
# ти працюєш» answer says «порахую й відповім»), so they bought a wasted second LLM turn
# — seconds of extra latency — on replies that were never stalled.
_ACTION_PROMISE_RE = re.compile(
    r"гля[нм]|підні[мж]|підтягн|зведу|зачекай",
    re.IGNORECASE,
)
_RETRY_NUDGE = (
    "\n\n[СИСТЕМА: твоя попередня відповідь лише пообіцяла дію (напр. «зараз гляну»), "
    "але не викликала інструмент — користувач лишиться без даних. Зараз АБО виклич "
    "потрібний інструмент (tool) з аргументами, АБО дай повну відповідь одразу, без "
    "обіцянок «зараз гляну».]"
)


def _promises_action(message: str) -> bool:
    """True if the persona line promises a fetch it never delivered (no tool).

    `SAFE_FALLBACK` is excluded explicitly: it's what `decide` returns when the LLM
    call itself failed (two timeouts, an outage). Retrying THAT is guaranteed waste —
    it means a second doomed round of `claude -p` calls, and the fallback's own wording
    used to match the regex, so an outage cost up to ~240s of «друкує…» before the user
    saw the error line.
    """
    if not message or message == SAFE_FALLBACK:
        return False
    return bool(_ACTION_PROMISE_RE.search(message))


def _topic_gen(args: dict) -> str:
    """The provider as a GENITIVE modifier, so lines read naturally in Ukrainian
    («показники води», not «лічильник воду»). Empty when there's nothing to name."""
    name = str((args or {}).get("provider_name") or "").casefold()
    if "інтернет" in name or "gigabit" in name:
        return "інтернету"
    if "мобільн" in name:
        return "мобільного"
    if "газ" in name:
        return "газу"
    if "вод" in name:
        return "води"
    if "світл" in name or "електро" in name:
        return "світла"
    if "дах" in name or "кварт" in name:
        return "квартплати"
    return ""


def _progress_line(tool: str, args: dict) -> str:
    """A short, natural «I'm on it» line in the butler's voice — sent before the work so
    the reply doesn't echo the user's words back. Topic-aware where we can name the thing,
    varied so repeated asks never read like a canned autoreply."""
    topic = _topic_gen(args)
    if tool == "get_provider_balance":
        if topic == "мобільного":
            return random.choice(
                [
                    "Готую посилання на мобільний…",
                    "Збираю лінк на поповнення…",
                    "Зараз дам посилання на мобільний…",
                ]
            )
        return random.choice(
            [
                "Зазираю в кабінет інтернету…",
                "Дивлюся баланс інтернету…",
                "Перевіряю рахунок Gigabit+…",
                "Заглядаю, скільки на інтернеті…",
                "Звіряюся з кабінетом інтернету…",
            ]
        )
    if tool == "get_meter_history":
        if topic:
            return random.choice(
                [
                    f"Підіймаю показники {topic}…",
                    f"Дивлюся показники {topic}…",
                    f"Гортаю журнал {topic}…",
                ]
            )
        return random.choice(
            ["Підіймаю показники…", "Дивлюся показники…", "Гортаю журнал лічильників…"]
        )
    if tool == "get_meter_photo":
        if topic:
            return random.choice(
                [f"Шукаю фото лічильника {topic}…", f"Дістаю знімок {topic}…"]
            )
        return random.choice(["Шукаю збережене фото лічильника…", "Дістаю знімок…"])
    if tool == "get_unpaid":
        return random.choice(
            [
                "Гляну, що ще відкрито…",
                "Дивлюся, що лишилось цього місяця…",
                "Зараз перевірю рахунки…",
                "Звіряю список несплачених…",
                "Дивлюся, чи нема хвостів…",
            ]
        )
    if tool == "get_stats":
        return random.choice(
            [
                "Зводжу цифри…",
                "Підбиваю витрати…",
                "Рахую, секунду…",
                "Збираю статистику…",
                "Гортаю витрати по полицях…",
            ]
        )
    if tool == "get_stats_trend":
        if (args or {}).get("mode") == "volume":
            return random.choice(
                [
                    "Дивлюся, скільки намотало по місяцях…",
                    "Зводжу споживання по місяцях…",
                ]
            )
        return random.choice(
            [
                "Малюю динаміку по місяцях…",
                "Дивлюся, як воно мінялося…",
                "Вибудовую місяці в ряд…",
                "Зводжу помісячну картину…",
            ]
        )
    if tool == "get_payment_journal":
        return random.choice(
            [
                "Гортаю історію платежів…",
                "Дивлюся, коли й за що платив…",
                "Підіймаю дати оплат…",
            ]
        )
    if tool == "get_payment_plan":
        return random.choice(
            [
                "Складаю план оплат…",
                "Дивлюся, що, коли й через що платимо…",
                "Звіряю графік оплат…",
            ]
        )
    if tool == "log_payment_manual":
        return random.choice(
            ["Записую платіж…", "Фіксую…", "Заношу в журнал…", "Беру на олівець…"]
        )
    if tool == "categorize_payment":
        return random.choice(
            ["Розношу платіж по полицях…", "Розкладаю платіж куди слід…"]
        )
    if tool == "snooze_reminder":
        return random.choice(["Відкладаю нагадування…", "Переношу нагадування…"])
    if tool == "confirm_meter_reading":
        return random.choice(["Підтверджую показник…", "Фіксую показник…"])
    if tool == "delete_meter_reading":
        return random.choice(["Гляну, що саме прибрати…", "Дивлюся, що треба стерти…"])
    return random.choice(["Хвилинку…", "Зараз гляну…", "Момент…", "Уже беруся…"])


async def _retry_without_unknown_args(
    tool_fn: Callable, session: AsyncSession, decision: Decision, exc: TypeError
) -> dict | None:
    """Re-run a tool with only the kwargs it actually accepts. None if that can't help.

    The model occasionally invents an argument name. Everything else it chose — the
    tool, the period, the provider — is usually right, so dropping the unknown key
    rescues the turn instead of spending it on an apology.
    """
    try:
        accepted = set(inspect.signature(tool_fn).parameters)
    except (TypeError, ValueError):
        return None
    # `session` is ours to pass positionally — a model naming it as a kwarg would make
    # the retry a "multiple values for argument 'session'" TypeError, so treat it as
    # unknown however much the signature declares it.
    accepted.discard("session")
    known = {k: v for k, v in decision.args.items() if k in accepted}
    dropped = sorted(set(decision.args) - accepted)
    if not dropped:
        return None  # the TypeError was about something else — don't loop
    log.warning("tool %r: dropping unknown args %s (%s)", decision.tool, dropped, exc)
    try:
        return await tool_fn(session, **known)
    except TypeError as retry_exc:
        log.warning(
            "tool %r still failed after dropping args: %s", decision.tool, retry_exc
        )
        return None
    # ToolError / NotImplementedError deliberately propagate: the caller's own branches
    # phrase them properly («⚠️ <причина>» / «⏳ Це вміння — для наступної фази»), and
    # swallowing them here flattened both into a generic «не зрозумів параметри».


@dataclass
class Reply:
    text: str
    chart_path: str | None = None
    tool: str | None = None
    tool_result: dict | None = None
    error: str | None = None
    meta: dict = field(default_factory=dict)


async def build_context(session: AsyncSession) -> dict:
    """Snapshot the agent reasons over: open obligations + recent payments."""
    unpaid = await tools_mod.get_unpaid(session)

    recent_rows = (
        (await session.execute(select(Payment).order_by(Payment.paid_at.desc()).limit(5)))
        .scalars()
        .all()
    )
    # ONE providers query, used for both the name lookup and the catalogue below. It ran
    # twice before, on the path that precedes every single LLM call.
    provs = list((await session.execute(select(Provider))).scalars())
    prov_names = {p.id: p.name for p in provs}
    recent = [
        {
            "provider": prov_names.get(p.provider_id) if p.provider_id else None,
            "amount_uah": str(p.amount_uah),
            "paid_at": p.paid_at.isoformat(),
            "source": p.source.value,
        }
        for p in recent_rows
    ]

    # `due_day` = day of the month each provider is due (None = no scheduled payment,
    # e.g. mobile autopay or the unoccupied flat). Carried so the agent can answer
    # «коли платимо» from real data instead of claiming it has none.
    providers = [
        {
            "name": prov.name,
            "category": prov.category.value,
            "due_day": prov.due_day,
        }
        for prov in provs
    ]

    meter_rows = (
        (
            await session.execute(
                select(MeterReading)
                .where(
                    MeterReading.status.in_(
                        (MeterStatus.validated, MeterStatus.submitted)
                    )
                )
                .order_by(MeterReading.created_at.desc())
                .limit(5)
            )
        )
        .scalars()
        .all()
    )
    meters = [
        {
            "provider": prov_names.get(m.provider_id) if m.provider_id else None,
            "cycle": m.cycle,
            "value": str(m.value) if m.value is not None else None,
            "status": m.status.value,
        }
        for m in meter_rows
    ]

    return {
        "cycle": clock.current_cycle(),
        "unpaid": unpaid,
        "recent_payments": recent,
        "providers": providers,
        "autopay_day": get_settings().mobile_autopay_day,
        "meters": meters,
    }


async def handle_message(
    user_text: str,
    session: AsyncSession,
    llm: LLMProvider,
    *,
    history: list[dict] | None = None,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> Reply:
    context = await build_context(session)
    if history:
        # Last few turns so the model can resolve short replies («давай», «а за травень?»)
        # against its own previous line instead of restarting from a blank slate.
        context["recent_dialogue"] = history
    decision: Decision = await llm.decide(user_text, context)

    # Guard against a stalled promise: if the reply pledges to look something up but
    # picked no tool, re-ask once so the model either calls the tool or answers in full.
    # (This is what dead-ended «давай по воді» → «зараз підніму показники» with no data.)
    if not decision.tool and _promises_action(decision.message):
        log.info("LLM promised an action without a tool — re-asking once")
        decision = await llm.decide(user_text + _RETRY_NUDGE, context)

    # No tool → the persona reply stands on its own.
    if not decision.tool:
        return Reply(text=decision.message)

    tool_fn = tools_mod.TOOLS.get(decision.tool)
    if tool_fn is None:
        log.warning("LLM chose unknown tool %r; ignoring", decision.tool)
        return Reply(text=decision.message, error=f"unknown tool {decision.tool}")

    # Acknowledge naturally before doing the work («зазираю в кабінет інтернету…»)
    # instead of echoing the user's words back. Only when the caller opted in (voice).
    if on_progress is not None:
        await on_progress(_progress_line(decision.tool, decision.args))

    def _tool_error(exc: Exception) -> Reply:
        """The user-facing phrasing for a tool that refused the call."""
        if isinstance(exc, NotImplementedError):
            return Reply(
                text=f"{decision.message}\n\n⏳ Це вміння — для наступної фази.".strip(),
                tool=decision.tool,
                error=str(exc),
            )
        # ToolError is user-correctable — surface its reason alongside the persona line.
        return Reply(
            text=f"{decision.message}\n\n⚠️ {exc}".strip(),
            tool=decision.tool,
            error=str(exc),
        )

    try:
        result = await tool_fn(session, **decision.args)
    except (ToolError, NotImplementedError) as exc:
        return _tool_error(exc)
    except TypeError as exc:  # bad/missing args from the model
        # A hallucinated arg name (get_stats(months=6)) used to end here silently: the
        # user got the persona preamble and NO data, with no way to tell a failure from a
        # stalled bot. Drop the args the tool doesn't take and run it once more — the
        # remaining args usually carry the intent. Only if that fails do we say so.
        try:
            retried = await _retry_without_unknown_args(tool_fn, session, decision, exc)
        except (ToolError, NotImplementedError) as retry_exc:
            # The retry reached the tool and IT objected — that reason is worth more to
            # the user than a generic «не зрозумів параметри».
            return _tool_error(retry_exc)
        if retried is None:
            return Reply(
                text=f"{decision.message}\n\n⚠️ Не зрозумів параметри запиту — "
                "переформулюй, будь ласка.".strip(),
                tool=decision.tool,
                error=str(exc),
            )
        result = retried

    # Surface the tool's own answer. Some tools compute data the LLM didn't have when
    # it wrote `message` (e.g. a scraped balance, a stored meter reading) and return it
    # as result["message"]; append it so the user actually sees the result, not just the
    # persona preamble. Tools that put their data in the LLM context (get_unpaid/stats)
    # return no "message" and are unaffected.
    chart_path = result.get("chart_path") if isinstance(result, dict) else None
    result_msg = result.get("message") if isinstance(result, dict) else None
    if on_progress is not None:
        # We already acknowledged with a progress line, so the reply is just the payload —
        # no «зараз гляну» preamble to double up on what we just said.
        text = (result_msg or decision.message or "").strip()
    else:
        text = decision.message or ""
        if result_msg:
            text = f"{text}\n\n{result_msg}".strip()
    return Reply(
        text=text,
        chart_path=chart_path,
        tool=decision.tool,
        tool_result=result if isinstance(result, dict) else None,
    )
