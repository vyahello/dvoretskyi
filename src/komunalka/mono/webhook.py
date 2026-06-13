"""monobank webhook receiver + the core categorize-and-learn processing.

The processing function `process_statement_item` is bot-agnostic and returns a
`ProcessResult`; the FastAPI router applies it and then notifies Telegram. This keeps
the categorization logic unit-testable without a live Bot.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass

from fastapi import APIRouter, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from komunalka.config import get_settings
from komunalka.db.models import Payment, PaymentSource, Provider
from komunalka.db.session import session_scope
from komunalka.mono import matcher
from komunalka.mono.schemas import StatementItem, WebhookPayload

log = logging.getLogger(__name__)
router = APIRouter()


class Action(str, enum.Enum):
    LOGGED = "logged"                  # matched a provider, payment recorded
    UNCATEGORIZED = "uncategorized"    # utility candidate, needs a categorize prompt
    DUPLICATE = "duplicate"            # mono_tx_id already seen
    INFLOW = "inflow"                  # top-up/refund, ignored
    NOT_CANDIDATE = "not_candidate"    # not комуналка (e.g. groceries), ignored


@dataclass
class ProcessResult:
    action: Action
    payment: Payment | None = None
    provider: Provider | None = None


async def process_statement_item(
    session: AsyncSession, item: StatementItem
) -> ProcessResult:
    """Idempotent, outflow-only categorize-and-learn for one transaction."""
    # 3. Idempotency.
    existing = (
        await session.execute(
            select(Payment).where(Payment.mono_tx_id == item.id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return ProcessResult(Action.DUPLICATE, payment=existing, provider=None)

    # 4. Outflows only.
    if not item.is_outflow:
        return ProcessResult(Action.INFLOW)

    # 5. Match against known provider patterns.
    provider = await matcher.match(session, item.description)
    if provider is not None:
        payment = Payment(
            provider_id=provider.id,
            amount_uah=item.amount_uah,
            paid_at=item.paid_at,
            source=PaymentSource.mono_webhook,
            raw_description=item.description,
            mcc=item.mcc,
            mono_tx_id=item.id,
        )
        session.add(payment)
        await session.flush()
        return ProcessResult(Action.LOGGED, payment=payment, provider=provider)

    # 5b. Unmatched → only act if it looks like комуналка.
    if not matcher.is_utility_candidate(item.mcc, item.description):
        return ProcessResult(Action.NOT_CANDIDATE)

    # Candidate: store uncategorized, prompt the user to categorize.
    payment = Payment(
        provider_id=None,
        amount_uah=item.amount_uah,
        paid_at=item.paid_at,
        source=PaymentSource.mono_webhook,
        raw_description=item.description,
        mcc=item.mcc,
        mono_tx_id=item.id,
    )
    session.add(payment)
    await session.flush()
    return ProcessResult(Action.UNCATEGORIZED, payment=payment, provider=None)


@router.get("/mono/webhook/{secret}")
async def validate_webhook(secret: str) -> Response:
    """mono sends a GET to validate the URL on registration."""
    if secret != get_settings().mono_webhook_secret:
        return Response(status_code=403)
    return Response(status_code=200)


@router.post("/mono/webhook/{secret}")
async def receive_webhook(secret: str, request: Request) -> Response:
    if secret != get_settings().mono_webhook_secret:
        return Response(status_code=403)

    try:
        payload = WebhookPayload.model_validate(await request.json())
    except Exception:
        log.warning("mono webhook: unparseable payload", exc_info=True)
        return Response(status_code=200)  # ack to avoid mono retries on junk

    if payload.type != "StatementItem":
        return Response(status_code=200)

    item = payload.data.statementItem
    async with session_scope() as session:
        result = await process_statement_item(session, item)
        # Capture notification data inside the session (objects expire after commit).
        notice = _build_notice(result)

    notifier = getattr(request.app.state, "notifier", None)
    if notice is not None and notifier is not None:
        await notifier(notice)

    return Response(status_code=200)


@dataclass
class Notice:
    """What to tell the user about a processed tx (resolved before session close)."""

    action: Action
    payment_id: int | None = None
    amount_uah: str | None = None
    provider_name: str | None = None
    mono_tx_id: str | None = None
    raw_description: str | None = None


def _build_notice(result: ProcessResult) -> Notice | None:
    if result.action is Action.LOGGED and result.payment is not None:
        return Notice(
            action=Action.LOGGED,
            payment_id=result.payment.id,
            amount_uah=str(result.payment.amount_uah),
            provider_name=result.provider.name if result.provider else None,
            mono_tx_id=result.payment.mono_tx_id,
        )
    if result.action is Action.UNCATEGORIZED and result.payment is not None:
        return Notice(
            action=Action.UNCATEGORIZED,
            payment_id=result.payment.id,
            amount_uah=str(result.payment.amount_uah),
            mono_tx_id=result.payment.mono_tx_id,
            raw_description=result.payment.raw_description,
        )
    return None
