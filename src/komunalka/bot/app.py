"""aiogram 3 bot: allowlist middleware, free-text handler, inline-button handlers.

Button taps call tools directly (deterministic), never through the LLM. The webhook
notifier (used by FastAPI) is built here too, so confirmations/prompts share the Bot.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.types import CallbackQuery, FSInputFile, Message, TelegramObject
from sqlalchemy import select

from komunalka.agent import dispatcher as agent_dispatcher
from komunalka.agent.provider import get_provider
from komunalka.agent.tools import ToolError, categorize_payment, snooze_reminder
from komunalka.bot import keyboards
from komunalka.config import get_settings
from komunalka.db.models import Payment, Provider
from komunalka.db.session import session_scope
from komunalka.mono.webhook import Action, Notice

log = logging.getLogger(__name__)
router = Router()


class AllowlistMiddleware(BaseMiddleware):
    """Silently drop any update not from the single authorized user."""

    def __init__(self, allowed_user_id: int) -> None:
        self.allowed_user_id = allowed_user_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id != self.allowed_user_id:
            return None  # drop, no reply
        return await handler(event, data)


@router.message(F.text)
async def on_text(message: Message) -> None:
    async with session_scope() as session:
        reply = await agent_dispatcher.handle_message(
            message.text or "", session, get_provider()
        )
    await message.answer(reply.text or "…")
    if reply.chart_path:
        await message.answer_photo(FSInputFile(reply.chart_path))


async def _edit(callback: CallbackQuery, text: str) -> None:
    """Edit the originating message if it's still accessible; else send a fresh one."""
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text)
    elif callback.bot is not None and callback.message is not None:
        await callback.bot.send_message(callback.message.chat.id, text)


@router.callback_query(F.data.startswith("c:"))
async def on_categorize(callback: CallbackQuery) -> None:
    if not callback.data:
        await callback.answer()
        return
    _, payment_id_s, choice = callback.data.split(":", 2)
    payment_id = int(payment_id_s)

    async with session_scope() as session:
        payment = await session.get(Payment, payment_id)
        if payment is None:
            await callback.answer("Платіж уже зник.")
            return

        if choice == "n":  # «Не комуналка» → drop it
            await session.delete(payment)
            text = "Гаразд, викреслив. Не кожна витрата — комуналка."
        else:
            provider = await session.get(Provider, int(choice))
            if provider is None or payment.mono_tx_id is None:
                await callback.answer("Не вдалося.")
                return
            result = await categorize_payment(session, payment.mono_tx_id, provider.name)
            text = (
                f"✅ {result['provider']} — {result['amount_uah']} ₴, "
                "записав і запам'ятав."
            )

    await _edit(callback, text)
    await callback.answer()


@router.callback_query(F.data.startswith("s:"))
async def on_snooze(callback: CallbackQuery) -> None:
    if not callback.data:
        await callback.answer()
        return
    _, provider_id_s, days_s = callback.data.split(":", 2)
    async with session_scope() as session:
        provider = await session.get(Provider, int(provider_id_s))
        if provider is None:
            await callback.answer("Провайдера нема.")
            return
        try:
            result = await snooze_reminder(session, provider.name, days_s)
        except ToolError as exc:
            await callback.answer(str(exc))
            return
    await _edit(
        callback, f"Відклав нагадування по «{result['provider']}». Повернуся пізніше."
    )
    await callback.answer()


def build_dispatcher() -> Dispatcher:
    settings = get_settings()
    dp = Dispatcher()
    dp.update.outer_middleware(AllowlistMiddleware(settings.telegram_allowed_user_id))
    dp.include_router(router)
    return dp


def build_bot() -> Bot:
    return Bot(token=get_settings().telegram_bot_token)


# --- webhook → Telegram notifier ------------------------------------------


def make_notifier(bot: Bot) -> Callable[[Notice], Awaitable[None]]:
    """An async callable the mono webhook uses to push confirmations/prompts."""
    settings = get_settings()
    chat_id = settings.telegram_allowed_user_id

    async def notify(notice: Notice) -> None:
        if notice.action is Action.LOGGED:
            await bot.send_message(
                chat_id,
                f"✅ {notice.provider_name} — {notice.amount_uah} ₴, записав.",
            )
        elif notice.action is Action.UNCATEGORIZED and notice.payment_id is not None:
            async with session_scope() as session:
                providers = (
                    (await session.execute(select(Provider).order_by(Provider.id)))
                    .scalars()
                    .all()
                )
            kb = keyboards.categorize_keyboard(notice.payment_id, providers)
            await bot.send_message(
                chat_id,
                f"Прилетіло {notice.amount_uah} ₴ — не з мого списку. Це що?",
                reply_markup=kb,
            )

    return notify
