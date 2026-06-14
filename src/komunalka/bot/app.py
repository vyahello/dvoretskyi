"""aiogram 3 bot: allowlist middleware, free-text handler, inline-button handlers.

Button taps call tools directly (deterministic), never through the LLM. The webhook
notifier (used by FastAPI) is built here too, so confirmations/prompts share the Bot.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    Message,
    TelegramObject,
)
from sqlalchemy import select

from komunalka import clock
from komunalka.agent import dispatcher as agent_dispatcher
from komunalka.agent.provider import get_provider
from komunalka.agent.tools import (
    ToolError,
    categorize_payment,
    get_stats,
    get_unpaid,
    snooze_reminder,
)
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


# --- slash commands (deterministic — no LLM) ------------------------------

HELP_TEXT = (
    "До ваших послуг. Веду вашу комуналку: фіксую платежі, рахую статистику й "
    "нагадую про дедлайни.\n\n"
    "Команди:\n"
    "/unpaid — що ще відкрите цього місяця\n"
    "/stats — витрати за місяць (графіком, якщо є що показати)\n"
    "/help — це повідомлення\n\n"
    "А ще просто пишіть як людині — напр. «що треба заплатити?», «скільки вийшло "
    "за травень?» або «відклади воду на три дні». Розберуся."
)


def _format_unpaid(result: dict) -> str:
    """Compact butler-voice rendering of get_unpaid output."""
    if result.get("all_clear"):
        return "✅ Усе чисто — цього місяця все оплачено."
    lines = ["Відкрите цього місяця:"]
    for item in result["open"]:
        amount = (
            f" (≈{item['expected_amount']} ₴)"
            if item.get("expected_amount") is not None
            else ""
        )
        due = f" — до {item['due_day']}-го" if item.get("due_day") else ""
        lines.append(f"• {item['provider']}{amount}{due}")
    return "\n".join(lines)


def _format_stats(result: dict) -> str:
    """Compact butler-voice rendering of get_stats output (header line)."""
    if not result["items"]:
        return f"{result['period']}: ще порожньо — жодного платежу."
    top = result["items"][0]
    return (
        f"{result['period']} — {result['total']} ₴. "
        f"Найбільше з'їв {top['label']} ({top['total']} ₴)."
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "До ваших послуг — ваш комунальний дворецький. Стежу за платежами, "
        "статистикою та дедлайнами.\n"
        "Почніть з /unpaid, або просто спитайте «що треба заплатити?». "
        "Повний перелік — /help."
    )


@router.message(Command("unpaid"))
async def cmd_unpaid(message: Message) -> None:
    async with session_scope() as session:
        result = await get_unpaid(session)
    await message.answer(_format_unpaid(result))


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    async with session_scope() as session:
        result = await get_stats(
            session, period=clock.current_cycle(), breakdown="provider"
        )
    text = _format_stats(result)
    if result.get("chart_path"):
        await message.answer_photo(FSInputFile(result["chart_path"]), caption=text)
    else:
        await message.answer(text)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(F.text)
async def on_text(message: Message) -> None:
    try:
        async with session_scope() as session:
            reply = await agent_dispatcher.handle_message(
                message.text or "", session, get_provider()
            )
    except Exception:
        # Anything from context-building, the LLM path, or the DB lands here.
        # Log the traceback (otherwise it's swallowed → silent Telegram) and still
        # reply, so the user never faces dead air.
        log.exception("on_text failed for message %r", message.text)
        await message.answer(
            "Щось у моїх паперах заклинило — спробуйте ще раз за мить."
        )
        return
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


# Mirror of the BotFather menu, kept in code so it stays in sync.
BOT_COMMANDS = [
    BotCommand(command="start", description="Привітання дворецького"),
    BotCommand(command="unpaid", description="Що ще не оплачено цього місяця"),
    BotCommand(command="stats", description="Витрати за поточний місяць"),
    BotCommand(command="help", description="Що я вмію і як зі мною говорити"),
]


async def set_my_commands(bot: Bot) -> None:
    await bot.set_my_commands(BOT_COMMANDS)


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
                f"Прилетіло {notice.amount_uah} ₴, а такого в мене нема. Це що?",
                reply_markup=kb,
            )

    return notify
