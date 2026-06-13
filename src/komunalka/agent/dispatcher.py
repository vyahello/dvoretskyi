"""Free-text → LLM decision → deterministic tool routing → Платон's reply.

Tool routing is deterministic (Python decides whether/which TOOL runs based on the
LLM's structured choice). The persona governs only `message`; we never let the model
fabricate tool *results*.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from komunalka import clock
from komunalka.agent import tools as tools_mod
from komunalka.agent.provider import Decision, LLMProvider
from komunalka.agent.tools import ToolError
from komunalka.db.models import Payment, Provider

log = logging.getLogger(__name__)


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

    return {
        "cycle": clock.current_cycle(),
        "unpaid": unpaid,
        "recent_payments": recent,
        "providers": providers,
    }


async def handle_message(
    user_text: str, session: AsyncSession, llm: LLMProvider
) -> Reply:
    context = await build_context(session)
    decision: Decision = await llm.decide(user_text, context)

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

    # Deterministic: persona message passes through untouched; tool data rides along.
    chart_path = result.get("chart_path") if isinstance(result, dict) else None
    return Reply(
        text=decision.message,
        chart_path=chart_path,
        tool=decision.tool,
        tool_result=result if isinstance(result, dict) else None,
    )
