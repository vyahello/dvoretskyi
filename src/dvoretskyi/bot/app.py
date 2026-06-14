"""aiogram 3 bot: allowlist middleware, free-text handler, inline-button handlers.

Button taps call tools directly (deterministic), never through the LLM. The webhook
notifier (used by FastAPI) is built here too, so confirmations/prompts share the Bot.
"""

from __future__ import annotations

import logging
import os
import random
import tempfile
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from sqlalchemy import select

from dvoretskyi import clock
from dvoretskyi.agent import dispatcher as agent_dispatcher
from dvoretskyi.agent.provider import get_provider
from dvoretskyi.agent.tools import (
    ToolError,
    categorize_payment,
    confirm_meter_reading,
    get_meter_history,
    get_provider_balance,
    get_stats,
    get_unpaid,
    mark_meter_submitted,
    snooze_reminder,
    submit_meter_reading,
)
from dvoretskyi.agent.vision import get_vision_provider
from dvoretskyi.bot import keyboards
from dvoretskyi.config import get_settings
from dvoretskyi.db.models import (
    MeterReading,
    MeterStatus,
    NudgeKind,
    NudgeLog,
    Payment,
    Provider,
)
from dvoretskyi.db.session import session_scope
from dvoretskyi.mono.webhook import Action, Notice

# Private dir for in-flight meter photos; files are deleted right after processing.
_MEDIA_DIR = Path(tempfile.gettempdir()) / "dvoretskyi_meters"

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
    auto = result.get("auto_pending") or []
    auto_note = ""
    if auto:
        names = ", ".join(i["provider"] for i in auto)
        day = auto[0].get("autopay_day") or get_settings().mobile_autopay_day
        auto_note = f"\n⏳ {names} — автосписанням monobank {day}-го, ще не пройшло."

    if result.get("all_clear"):
        head = (
            "✅ Усе, що треба, оплачено."
            if auto
            else "✅ Усе чисто — цього місяця все оплачено."
        )
        return head + auto_note
    lines = ["Відкрите цього місяця:"]
    for item in result["open"]:
        amount = (
            f" (≈{item['expected_amount']} ₴)"
            if item.get("expected_amount") is not None
            else ""
        )
        due = f" — до {item['due_day']}-го" if item.get("due_day") else ""
        lines.append(f"• {item['provider']}{amount}{due}")
    return "\n".join(lines) + auto_note


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
        "Тапай кнопки внизу або просто пиши як людині — напр. «що треба заплатити?». "
        "Повний перелік — /help.",
        reply_markup=keyboards.main_keyboard(),
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


# --- main-menu reply-keyboard taps (registered BEFORE the free-text catch-all) ---


@router.message(F.text == keyboards.MENU_UNPAID)
async def menu_unpaid(message: Message) -> None:
    await cmd_unpaid(message)


@router.message(F.text == keyboards.MENU_STATS)
async def menu_stats(message: Message) -> None:
    await cmd_stats(message)


@router.message(F.text == keyboards.MENU_HELP)
async def menu_help(message: Message) -> None:
    await cmd_help(message)


@router.message(F.text == keyboards.MENU_BALANCE)
async def menu_balance(message: Message) -> None:
    async with session_scope() as session:
        res = await get_provider_balance(session, "Інтернет (Gigabit+)")
    pay_link = res.get("pay_link")
    markup = (
        keyboards.pay_keyboard(pay_link, label=res.get("pay_label")) if pay_link else None
    )
    await message.answer(res.get("message") or "…", reply_markup=markup)


_UA_MONTHS = (
    "",
    "січень",
    "лютий",
    "березень",
    "квітень",
    "травень",
    "червень",
    "липень",
    "серпень",
    "вересень",
    "жовтень",
    "листопад",
    "грудень",
)


def _format_cycle(cycle: str) -> str:
    """'2026-06' → 'червень 2026'; fall back to the raw key if it's malformed."""
    try:
        year, month = cycle.split("-")
        return f"{_UA_MONTHS[int(month)]} {year}"
    except (ValueError, IndexError):
        return cycle


def _format_meters_overview(overview: list[tuple[str, list[dict]]]) -> str:
    """Render the monthly meter journal I keep — one block per meter provider."""
    blocks: list[str] = []
    for name, readings in overview:
        if not readings:
            continue
        lines = [name]
        for r in readings:  # newest-first from get_meter_history
            tail = f"  (спожито {r['consumption']})" if r.get("consumption") else ""
            lines.append(f"• {_format_cycle(r['cycle'])}: {r['value']}{tail}")
        blocks.append("\n".join(lines))
    if not blocks:
        return (
            "Поки що показників нема — журнал чистий. 🔢\n"
            "Кинь фото лічильника, і я зчитаю та збережу його щомісяця."
        )
    return "🔢 Показники, що я зберіг:\n\n" + "\n\n".join(blocks)


@router.message(F.text == keyboards.MENU_METERS)
async def menu_meters(message: Message) -> None:
    async with session_scope() as session:
        overview: list[tuple[str, list[dict]]] = []
        for prov in await _meter_providers(session):
            hist = await get_meter_history(session, prov.name, limit=6)
            overview.append((prov.name, hist["readings"]))
    await message.answer(_format_meters_overview(overview))


# Varied butler greetings (instant, no LLM) for the «🎩 Привіт» tap.
_GREETINGS = (
    "До ваших послуг. Сьогодні гроші, показники чи просто світська бесіда?",
    "Вітаю. Комуналка під контролем, нерви бережемо. Чим допомогти?",
    "На місці, серед квитанцій і спокою. Що цікавить?",
    "Доброго. Лічильники тихі, рахунки чекають — як завжди. Слухаю.",
    "Вітаю вас. Тицяйте кнопку або питайте — розберемось.",
)


@router.message(F.text == keyboards.MENU_HELLO)
async def menu_hello(message: Message) -> None:
    await message.answer(random.choice(_GREETINGS))


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
        await message.answer("Щось у моїх паперах заклинило — спробуйте ще раз за мить.")
        return
    markup = None
    if reply.tool_result and reply.tool_result.get("pay_link"):
        markup = keyboards.pay_keyboard(
            reply.tool_result["pay_link"], label=reply.tool_result.get("pay_label")
        )
    await message.answer(reply.text or "…", reply_markup=markup)
    if reply.chart_path:
        await message.answer_photo(FSInputFile(reply.chart_path))


async def _edit(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit the originating message if it's still accessible; else send a fresh one."""
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=reply_markup)
    elif callback.bot is not None and callback.message is not None:
        await callback.bot.send_message(
            callback.message.chat.id, text, reply_markup=reply_markup
        )


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


# --- meter photos (L2) -----------------------------------------------------

_CAPTION_HINTS = {"газ": "gas", "вод": "water"}


async def _download_photo(message: Message) -> str:
    """Download the highest-res photo to the private media dir. Injectable in tests."""
    if not message.photo or message.bot is None:
        raise RuntimeError("no photo to download")
    photo = message.photo[-1]
    _MEDIA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _MEDIA_DIR / f"{photo.file_unique_id}.jpg"
    await message.bot.download(photo, destination=str(path))
    return str(path)


def _caption_provider(
    caption: str | None, meter_providers: list[Provider]
) -> Provider | None:
    """Route by a caption like «показники газу» → the matching meter provider."""
    if not caption:
        return None
    low = caption.casefold()
    for stem, cat in _CAPTION_HINTS.items():
        if stem in low:
            for prov in meter_providers:
                if prov.category.value == cat:
                    return prov
    return None


async def _meter_providers(session) -> list[Provider]:
    return list(
        (
            await session.execute(
                select(Provider)
                .where(Provider.meter_window.is_not(None))
                .order_by(Provider.id)
            )
        )
        .scalars()
        .all()
    )


def _meter_reply(result: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    """Map a pipeline result dict → (text, keyboard) in the butler's voice."""
    text = result.get("message") or "…"
    rid = result.get("reading_id")
    status = result.get("status")
    if status == MeterStatus.needs_confirm.value and rid is not None:
        return text, keyboards.meter_confirm_keyboard(rid)
    if status == MeterStatus.validated.value and rid is not None:
        return text, keyboards.meter_submitted_keyboard(rid)
    return text, None


@router.message(F.photo)
async def on_photo(message: Message) -> None:
    path: str | None = None
    try:
        path = await _download_photo(message)
        # One vision pass decides everything: it auto-detects the meter type by look —
        # dark meter → water, light meter → gas — or says "other" if it isn't a meter.
        read = await get_vision_provider().read_meter(path)
        if read.kind == "other":
            # Not a meter: just react to the photo with a light remark, store nothing.
            await message.answer(
                read.comment or "Гарне фото, але лічильника на ньому я не бачу. 🎩"
            )
            return

        async with session_scope() as session:
            meter_provs = await _meter_providers(session)
            if not meter_provs:
                await message.answer("Лічильників у списку нема — нема куди вносити.")
                return

            # A caption can still override; otherwise route by the auto-detected kind.
            chosen = _caption_provider(message.caption, meter_provs)
            if chosen is None and read.kind in ("water", "gas"):
                chosen = next(
                    (p for p in meter_provs if p.category.value == read.kind), None
                )

            if chosen is None:
                # Couldn't tell which meter — stash the photo and ask.
                reading = MeterReading(
                    cycle=clock.current_cycle(),
                    status=MeterStatus.ocr_pending,
                    created_at=clock.now(),
                    photo_ref=path,
                )
                session.add(reading)
                await session.flush()
                await message.answer(
                    "Який це лічильник?",
                    reply_markup=keyboards.meter_route_keyboard(reading.id, meter_provs),
                )
                path = None  # keep the file — the m: callback will OCR then delete it
                return

            result = await submit_meter_reading(
                session, chosen.name, path, vision=get_vision_provider(), read=read
            )
        text, kb = _meter_reply(result)
        await message.answer(text, reply_markup=kb)
    except Exception:
        log.exception("on_photo failed")
        await message.answer("Не зміг обробити фото — спробуй ще раз за мить.")
    finally:
        if path and os.path.exists(path):
            os.unlink(path)


@router.callback_query(F.data.startswith("m:"))
async def on_meter_route(callback: CallbackQuery) -> None:
    if not callback.data:
        await callback.answer()
        return
    _, rid_s, pid_s = callback.data.split(":", 2)
    path: str | None = None
    async with session_scope() as session:
        reading = await session.get(MeterReading, int(rid_s))
        provider = await session.get(Provider, int(pid_s))
        if reading is None or provider is None or not reading.photo_ref:
            await callback.answer("Фото вже зникло — надішли ще раз.")
            return
        path = reading.photo_ref
        result = await submit_meter_reading(
            session,
            provider.name,
            path,
            vision=get_vision_provider(),
            reading_id=reading.id,
        )
    text, kb = _meter_reply(result)
    await _edit(callback, text, kb)
    if path and os.path.exists(path):
        os.unlink(path)
    await callback.answer()


@router.callback_query(F.data.startswith("mc:"))
async def on_meter_confirm(callback: CallbackQuery) -> None:
    if not callback.data:
        await callback.answer()
        return
    _, rid_s, choice = callback.data.split(":", 2)
    async with session_scope() as session:
        reading = await session.get(MeterReading, int(rid_s))
        if reading is None:
            await callback.answer("Показник зник.")
            return
        if choice == "re":  # re-photograph → discard this reading
            reading.status = MeterStatus.rejected
            await _edit(callback, "Гаразд, перефотографуй ближче і надішли ще раз.")
            await callback.answer()
            return
        try:
            result = await confirm_meter_reading(session, reading.id)
        except ToolError as exc:
            await callback.answer(str(exc))
            return
    text, kb = _meter_reply(result)
    await _edit(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("ms:"))
async def on_meter_submitted(callback: CallbackQuery) -> None:
    if not callback.data:
        await callback.answer()
        return
    _, rid_s = callback.data.split(":", 1)
    async with session_scope() as session:
        try:
            result = await mark_meter_submitted(session, rid_s)
        except ToolError as exc:
            await callback.answer(str(exc))
            return
    await _edit(callback, result["message"])
    await callback.answer()


@router.callback_query(F.data.startswith("sm:"))
async def on_meter_snooze(callback: CallbackQuery) -> None:
    if not callback.data:
        await callback.answer()
        return
    _, pid_s, days_s = callback.data.split(":", 2)
    until = clock.now() + timedelta(days=int(days_s))
    cycle = clock.current_cycle()
    async with session_scope() as session:
        provider = await session.get(Provider, int(pid_s))
        if provider is None:
            await callback.answer("Провайдера нема.")
            return
        nudge = (
            await session.execute(
                select(NudgeLog).where(
                    NudgeLog.provider_id == provider.id,
                    NudgeLog.cycle == cycle,
                    NudgeLog.kind == NudgeKind.meter,
                )
            )
        ).scalar_one_or_none()
        if nudge is None:
            session.add(
                NudgeLog(
                    provider_id=provider.id,
                    cycle=cycle,
                    kind=NudgeKind.meter,
                    nudged_at=clock.now(),
                    snoozed_until=until,
                )
            )
        else:
            nudge.snoozed_until = until
    await _edit(callback, "Відклав нагадування про показники. Повернуся пізніше.")
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
