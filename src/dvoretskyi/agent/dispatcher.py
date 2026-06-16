"""Free-text → LLM decision → deterministic tool routing → the butler's reply.

Tool routing is deterministic (Python decides whether/which TOOL runs based on the
LLM's structured choice). The persona governs only `message`; we never let the model
fabricate tool *results*.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dvoretskyi import clock
from dvoretskyi.agent import tools as tools_mod
from dvoretskyi.agent.provider import Decision, LLMProvider
from dvoretskyi.agent.tools import ToolError
from dvoretskyi.db.models import MeterReading, MeterStatus, Payment, Provider

log = logging.getLogger(__name__)

# A reply that *promises* to fetch something («зараз гляну», «підніму показники»,
# «секунду») but carries no tool is a dead-end — the bot can't call back, so the user
# is left with a preamble and no data. When we see this shape we re-ask the model once
# (below), telling it to actually call the tool or answer in full without promises.
_ACTION_PROMISE_RE = re.compile(
    r"гля[нм]|підні[мж]|підтягн|пораху|зведу|за мить|секунд|хвилин|зачекай|момент",
    re.IGNORECASE,
)
_RETRY_NUDGE = (
    "\n\n[СИСТЕМА: твоя попередня відповідь лише пообіцяла дію (напр. «зараз гляну»), "
    "але не викликала інструмент — користувач лишиться без даних. Зараз АБО виклич "
    "потрібний інструмент (tool) з аргументами, АБО дай повну відповідь одразу, без "
    "обіцянок «зараз гляну».]"
)


def _promises_action(message: str) -> bool:
    """True if the persona line promises a fetch it never delivered (no tool)."""
    return bool(_ACTION_PROMISE_RE.search(message or ""))


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
    prov_names = {
        p.id: p.name for p in (await session.execute(select(Provider))).scalars()
    }
    recent = [
        {
            "provider": prov_names.get(p.provider_id) if p.provider_id else None,
            "amount_uah": str(p.amount_uah),
            "paid_at": p.paid_at.isoformat(),
            "source": p.source.value,
        }
        for p in recent_rows
    ]

    providers = [
        {"name": name, "category": prov.category.value}
        for prov in (await session.execute(select(Provider))).scalars()
        for name in (prov.name,)
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
        "meters": meters,
    }


async def handle_message(
    user_text: str,
    session: AsyncSession,
    llm: LLMProvider,
    *,
    history: list[dict] | None = None,
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

    try:
        result = await tool_fn(session, **decision.args)
    except ToolError as exc:
        # User-correctable; surface alongside the persona line.
        return Reply(
            text=f"{decision.message}\n\n⚠️ {exc}".strip(),
            tool=decision.tool,
            error=str(exc),
        )
    except NotImplementedError as exc:
        return Reply(
            text=f"{decision.message}\n\n⏳ Це вміння — для наступної фази.".strip(),
            tool=decision.tool,
            error=str(exc),
        )
    except TypeError as exc:  # bad/missing args from the model
        log.warning("tool %r bad args %r: %s", decision.tool, decision.args, exc)
        return Reply(text=decision.message, tool=decision.tool, error=str(exc))

    # Surface the tool's own answer. Some tools compute data the LLM didn't have when
    # it wrote `message` (e.g. a scraped balance, a stored meter reading) and return it
    # as result["message"]; append it so the user actually sees the result, not just the
    # persona preamble. Tools that put their data in the LLM context (get_unpaid/stats)
    # return no "message" and are unaffected.
    chart_path = result.get("chart_path") if isinstance(result, dict) else None
    result_msg = result.get("message") if isinstance(result, dict) else None
    text = decision.message or ""
    if result_msg:
        text = f"{text}\n\n{result_msg}".strip()
    return Reply(
        text=text,
        chart_path=chart_path,
        tool=decision.tool,
        tool_result=result if isinstance(result, dict) else None,
    )
