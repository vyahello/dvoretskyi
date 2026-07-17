"""aiogram 3 bot: allowlist middleware, free-text handler, inline-button handlers.

Button taps call tools directly (deterministic), never through the LLM. The webhook
notifier (used by FastAPI) is built here too, so confirmations/prompts share the Bot.
"""

from __future__ import annotations

import calendar
import html
import logging
import os
import random
import re
import tempfile
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    TelegramObject,
)
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy import func, select

from dvoretskyi import clock, households
from dvoretskyi.agent import dispatcher as agent_dispatcher
from dvoretskyi.agent import meters
from dvoretskyi.agent.infolviv import (
    InfolvivReading,
    InfolvivSubmitDisabled,
    InfolvivSubmitError,
    fetch_infolviv_readings,
    submit_infolviv_reading,
)
from dvoretskyi.agent.provider import get_provider
from dvoretskyi.agent.tools import (
    ToolError,
    categorize_payment,
    confirm_meter_reading,
    delete_meter_reading,
    execute_meter_delete,
    get_meter_history,
    get_meter_journal,
    get_meter_photo_by_id,
    get_payment_journal,
    get_payment_plan,
    get_provider_balance,
    get_stats,
    get_stats_trend,
    get_unpaid,
    mark_meter_submitted,
    meter_hints,
    snooze_reminder,
    submit_meter_reading,
)
from dvoretskyi.agent.transcription import get_transcription_provider
from dvoretskyi.agent.tts import get_tts_provider
from dvoretskyi.agent.vision import get_vision_provider
from dvoretskyi.bot import keyboards
from dvoretskyi.config import get_settings
from dvoretskyi.db.models import (
    Category,
    Household,
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
    """Silently drop any update not from an allowed user (the owner + any family)."""

    def __init__(self, allowed_user_ids: set[int]) -> None:
        self.allowed_user_ids = allowed_user_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id not in self.allowed_user_ids:
            return None  # drop, no reply
        return await handler(event, data)


# --- slash commands (deterministic — no LLM) ------------------------------

HELP_TEXT = (
    "До ваших послуг. Веду вашу комуналку: фіксую платежі, рахую статистику, "
    "стежу за показниками й нагадую про оплати.\n\n"
    "🎙 Можна ГОЛОСОМ. Надиктуйте голосове — я розшифрую й відповім теж голосом. "
    "Усе, що роблю текстом, працює й голосом (лише показники лічильників — фото: "
    "цифри на слух я б наплутав).\n\n"
    "Кнопки внизу екрана:\n"
    "💸 Що сплатити — що ще відкрите цього місяця\n"
    "📊 Статистика — витрати за місяць, з графіком\n"
    "🔢 Мої показники — поточні лічильники: подане й чернетки\n"
    "📜 Історія — коли що подавав і коли за що платив (з датами)\n"
    "🗓 Як платити — коли, скільки й через що платити, з посиланнями\n"
    "🌐 Баланс інтернету — баланс і абонплата Gigabit+\n"
    "❓ Довідка — це повідомлення\n\n"
    "А найкраще — просто пишіть або говоріть як людині:\n"
    "• «що треба заплатити?», «скільки вийшло за травень?»\n"
    "• «коли я платив за газ?», «як і коли платити за світло?»\n"
    "• «покажи показники газу», «витягни фото лічильника»\n\n"
    "Нагадаю про оплату за 5 днів до дедлайну — з посиланням, куди платити.\n"
    "Щоб подати показники — просто пришліть фото лічильника. Решту зроблю сам."
)


# Varied closings so «нічого не висить» never reads like a canned autoreply (the
# butler should sound alive even when the news is boring-good).
_ALL_CLEAR_LINES = (
    "✅ Усе чисто — цього місяця нічого не висить.",
    "✅ Жодного відкритого рахунку. Рідкісний спокій.",
    "✅ Усе закрито — комуналка мовчить, і це добре.",
    "✅ Боргів нема. Можна видихнути.",
    "✅ Порожньо у списку боргів — гарний знак.",
    "✅ Усе сплачено. Тиша і спокій на рахунках.",
    "✅ Жодних хвостів цього місяця. Живемо.",
    "✅ Рахунки закриті, совість чиста. Відпочиваємо.",
    "✅ Нічого не висить — рідкісна, але приємна картина.",
)
# When mobile autopay is still pending we must NOT claim «все оплачено» — these heads
# say «головне закрито» and the auto-note adds the caveat.
_ALL_CLEAR_WITH_AUTO = (
    "✅ Усе, що залежало від нас, закрито.",
    "✅ Ручні оплати позаду.",
    "✅ З рахунками розібралися.",
    "✅ Основне закрито.",
    "✅ Усе, що потребувало рук, зроблено.",
    "✅ Головне сплачено — лишилась дрібниця нижче.",
    "✅ Ручну частину закрили. Решта — на автоматі.",
    "✅ Свою роботу зробили, рахунки чисті.",
)
# Auto-note variants. Each keeps «автосписанням», the provider name and the «{day}-го»
# so the caveat is unambiguous however it's phrased.
_AUTO_NOTES = (
    "⏳ {names} — автосписанням monobank {day}-го, ще не пройшло.",
    "⏳ {names} піде автосписанням {day}-го (monobank) — чекаємо.",
    "⏳ Лишився {names}: автосписанням monobank {day}-го, поки не списалось.",
    "⏳ {names} піде автосписанням monobank {day}-го — ще в дорозі.",
    "⏳ За {names} не хвилюйтесь: автосписанням monobank {day}-го, чекаємо.",
    "⏳ Тільки {names} на черзі — автосписанням monobank {day}-го.",
)


def _format_unpaid(result: dict) -> str:
    """Compact butler-voice rendering of get_unpaid output (phrasing varied each call)."""
    auto = result.get("auto_pending") or []
    auto_note = ""
    if auto:
        names = ", ".join(i["provider"] for i in auto)
        day = auto[0].get("autopay_day") or get_settings().mobile_autopay_day
        auto_note = "\n" + random.choice(_AUTO_NOTES).format(names=names, day=day)

    if result.get("all_clear"):
        head = random.choice(_ALL_CLEAR_WITH_AUTO if auto else _ALL_CLEAR_LINES)
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
    """Render get_stats output. The breakdown now lives in the rendered table image, so
    the tool's `message` is just a one-line caption (period + grand total) — use it; only
    fall back to a terse line if a hand-built dict (e.g. a test) carries no `message`."""
    msg = result.get("message")
    if msg:
        return msg
    if not result.get("items"):
        return "Платежів не бачу — порожньо."
    top = result["items"][0]
    return f"{result['period']} — {result['total']} ₴. Найбільше: {top['label']}."


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "До ваших послуг — ваш комунальний дворецький. Стежу за платежами, "
        "статистикою та оплатами.\n"
        "🎙 Можна не лише писати, а й говорити — надиктуйте голосове, відповім теж "
        "голосом.\n"
        "Тапай кнопки внизу або просто пиши чи говори як людині — напр. «що треба "
        "заплатити?». Усе, що вмію, — за кнопкою «❓ Довідка».",
        reply_markup=keyboards.main_keyboard(),
    )


@router.message(Command("unpaid"))
async def cmd_unpaid(message: Message) -> None:
    async with session_scope() as session:
        result = await get_unpaid(session)
    await message.answer(_format_unpaid(result))


async def _households_list(session) -> list[tuple[str, str]]:
    return [
        (h.slug, h.name)
        for h in (
            await session.execute(select(Household).order_by(Household.is_primary.desc()))
        ).scalars()
    ]


async def _data_cycle_range(session) -> tuple[str, str]:
    """(first, last) cycle we have payments for. The month strip is bounded by this, so
    ◀ stops at the first month with data instead of walking back through empty months to
    the dawn of time. Empty journal → the current month at both ends."""
    stmt = select(func.min(Payment.paid_at), func.max(Payment.paid_at)).where(
        Payment.provider_id.is_not(None)
    )
    lo, hi = (await session.execute(stmt)).one()
    now_cycle = clock.current_cycle()
    if lo is None or hi is None:
        return now_cycle, now_cycle
    return clock.cycle_of(clock.ensure_aware(lo)), clock.cycle_of(clock.ensure_aware(hi))


async def _month_view(
    session, cycle: str, hh: str
) -> tuple[str, InlineKeyboardMarkup, dict]:
    """One month's table + the ◀/▶ strip. `hh` is a slug or '-' (both households).
    Returns (caption, keyboard, the raw get_stats result — it carries the chart path)."""
    household = None if hh == "-" else hh
    result = await get_stats(
        session, period=cycle, breakdown="provider", household=household
    )
    first, _last = await _data_cycle_range(session)
    now_cycle = clock.current_cycle()
    # Never offer a month we can't show anything for: nothing before the first payment,
    # nothing in the future. An arrow into an empty month is a dead end, not navigation.
    prev_cycle = clock.shift_cycle(cycle, -1)
    next_cycle = clock.shift_cycle(cycle, 1)
    households_list = await _households_list(session)
    kb = keyboards.stats_month_keyboard(
        cycle,
        hh,
        prev_cycle=prev_cycle if clock.months_between(first, prev_cycle) >= 0 else None,
        next_cycle=(
            next_cycle if clock.months_between(next_cycle, now_cycle) >= 0 else None
        ),
        multi_household=len(households_list) > 1,
    )
    return _format_stats(result), kb, result


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    async with _thinking(message), session_scope() as session:
        text, kb, result = await _month_view(session, clock.current_cycle(), "-")
    await _send_stats(message, text, kb, result)


async def _send_stats(
    message: Message, text: str, kb: InlineKeyboardMarkup | None, result: dict
) -> None:
    """Send a stats view as photo+caption (or text when there's no chart), deleting the
    rendered PNG once it's on its way — see `_send_chart`."""
    path = result.get("chart_path")
    if path:
        await _send_chart(message, path, caption=text, reply_markup=kb)
    else:
        await message.answer(_trim(text), reply_markup=kb)


_PERIOD_TAPS = {"cur", "prev", "6m", "year", "all"}


def _decode_period(token: str) -> str:
    """A period button → a `get_stats` period. Total by construction: an unknown token
    falls back to the current month rather than reaching the parser as garbage."""
    now = clock.current_cycle()
    if token == "prev":
        return clock.shift_cycle(now, -1)
    if token == "6m":
        return "6m"
    if token == "year":
        return now.split("-")[0]
    if token == "all":
        return "all"
    return now  # "cur" and anything unrecognised


@router.callback_query(F.data.startswith("st:"))
async def on_stats_nav(callback: CallbackQuery) -> None:
    """The 📊 stats surface: month strip, period chooser, dynamics, household scope.

    Every view EDITS THE MESSAGE IN PLACE (like `on_history_nav`), so tapping through
    months doesn't bury the chat under a stack of near-identical charts.
    """
    data = callback.data or ""
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    parts = data.split(":")
    verb = parts[1] if len(parts) > 1 else ""

    # Legacy 2-part form (st:<slug> / st:split) still sitting on messages sent before
    # this deploy — keep them working rather than dying on a stale tap.
    if len(parts) == 2 and verb not in ("m", "t", "p", "P", "h", "H"):
        verb, parts = "H", ["st", "H", verb, clock.current_cycle()]

    def _arg(i: int, default: str = "-") -> str:
        """A callback field, tolerating a truncated/stale payload."""
        return parts[i] if len(parts) > i and parts[i] else default

    async with session_scope() as session:
        if verb == "m":  # month view
            text, kb, result = await _month_view(
                session, _arg(2, clock.current_cycle()), _arg(3)
            )
            await _replace_stats(callback, text, kb, result)
        elif verb == "p":  # period chooser
            await _edit_caption(
                callback, "📆 За який період?", keyboards.stats_period_keyboard(_arg(2))
            )
        elif verb == "P":  # apply a period
            token, hh = _arg(2, "cur"), _arg(3)
            period = _decode_period(token if token in _PERIOD_TAPS else "cur")
            if token in ("cur", "prev"):
                text, kb, result = await _month_view(session, period, hh)
            else:
                # A multi-month period is a trend by nature → show it by month.
                result = await get_stats(
                    session,
                    period=period,
                    breakdown="month",
                    household=None if hh == "-" else hh,
                )
                text = _format_stats(result)
                kb = keyboards.stats_month_keyboard(
                    clock.current_cycle(),
                    hh,
                    multi_household=len(await _households_list(session)) > 1,
                )
            await _replace_stats(callback, text, kb, result)
        elif verb == "t":  # dynamics
            mode, hh = _arg(2, "money"), _arg(3)
            result = await get_stats_trend(
                session,
                mode={"money": "money", "prov": "provider", "vol": "volume"}.get(
                    mode, "money"
                ),
                household=None if hh == "-" else hh,
            )
            await _replace_stats(
                callback,
                result.get("message") or "…",
                keyboards.stats_trend_keyboard(mode, hh),
                result,
            )
        elif verb == "h":  # household chooser
            # Carry the month being VIEWED into the chooser (`st:h:<hh>:<cycle>`), so
            # picking a property from a past month stays in that month. Hardcoding the
            # current cycle silently teleported the user back to today.
            await _edit_caption(
                callback,
                "🏘 Яке житло?",
                keyboards.stats_household_keyboard(
                    await _households_list(session), _arg(3, clock.current_cycle())
                ),
            )
        elif verb == "H":  # apply a household scope
            slug, cycle = _arg(2), _arg(3, clock.current_cycle())
            if slug == "split":
                result = await get_stats(session, period=cycle, breakdown="household")
                await _replace_stats(
                    callback,
                    _format_stats(result),
                    keyboards.stats_month_keyboard(cycle, "-"),
                    result,
                )
            else:
                text, kb, result = await _month_view(session, cycle, slug)
                await _replace_stats(callback, text, kb, result)
    await callback.answer()


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
    # On a cold cache this logs into the Gigabit+ cabinet — four sequential HTTP
    # round-trips. Without the indicator the chat just sat there, indistinguishable from
    # a dead bot.
    async with _thinking(message), session_scope() as session:
        res = await get_provider_balance(session, "Інтернет (Gigabit+)")
    pay_link = res.get("pay_link")
    markup = (
        keyboards.pay_keyboard(pay_link, label=res.get("pay_label")) if pay_link else None
    )
    await message.answer(_trim(res.get("message") or "…"), reply_markup=markup)


def _format_cycle(cycle: str) -> str:
    """'2026-06' → 'червень 2026'; fall back to the raw key if it's malformed."""
    return clock.format_cycle(cycle)


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


_KIND_LABEL = {"water": "💧 Холодна вода", "gas": "🔥 Газ"}


def _submission_window_label(now: datetime | None = None) -> str:
    """End-of-month submission window: from `meter_submit_from_day` to the real last day
    of the month, e.g. «28–30» (June), «28–31» (July), «28–29» (Feb leap). The last day
    comes from the calendar — never hardcoded, so it handles 28/29/30/31."""
    now = now or clock.now()
    last_day = calendar.monthrange(now.year, now.month)[1]
    start = min(get_settings().meter_submit_from_day, last_day)
    if start >= last_day:
        return f"{last_day} число місяця"
    return f"{start}–{last_day} число місяця"


async def _household_naming() -> tuple[dict[str, str], str | None]:
    """({infolviv account code → household name}, primary household name).

    The household's `infolviv_account_code` is its GAS account, so the two «🔥 Газ»
    counters are told apart by it. Other counters (water, etc.) live only at the primary
    property, so anything whose account isn't a known gas account belongs to the primary —
    hence we also return the primary name as the fallback. Addresses come from env→DB."""
    async with session_scope() as session:
        rows = (await session.execute(select(Household))).scalars().all()
    account_map = {
        h.infolviv_account_code: h.name for h in rows if h.infolviv_account_code
    }
    primary = next((h.name for h in rows if h.is_primary), None)
    return account_map, primary


def _format_infolviv_readings(
    readings: list[InfolvivReading],
    account_names: dict[str, str] | None = None,
    primary_name: str | None = None,
) -> str:
    """Render the readings filed on the infolviv portal — the authoritative record.

    Returns HTML (sent with parse_mode="HTML"): the «рахунок» is wrapped in a <code>
    span so Telegram doesn't auto-link the long digit run as a phone number. Every block
    names its property: gas counters by their account code, everything else (water, …)
    by the primary household — so «Холодна вода» also reads « · <дім>».
    """
    account_names = account_names or {}
    window = _submission_window_label()
    blocks: list[str] = []
    for r in readings:
        label = html.escape(_KIND_LABEL.get(r.kind, f"🔢 {r.service or 'Лічильник'}"))
        hh_name = account_names.get(r.account_code) or primary_name
        if hh_name:
            label = f"{label} · {html.escape(hh_name)}"
        num = f" (№<code>{html.escape(r.account_code)}</code>)" if r.account_code else ""
        lines = [f"{label}{num}"]
        if r.value is not None:
            period = _format_cycle(r.period) if r.period else "останній період"
            cons = f"  (спожито {r.difference})" if r.difference is not None else ""
            lines.append(f"• {period}: {r.value}{cons}")
        else:
            lines.append("• показника ще нема")
        lines.append(f"🗓 подача: {window}")
        blocks.append("\n".join(lines))
    return "🔢 Показники з порталу infolviv:\n\n" + "\n\n".join(blocks)


async def _local_journal() -> str:
    """My own photo-journal — the fallback when the portal is unreachable."""
    async with session_scope() as session:
        overview: list[tuple[str, list[dict]]] = []
        for prov in await _meter_providers(session):
            # Local-only: this is the fallback for when the portal is unreachable, so it
            # must not try infolviv again.
            hist = await get_meter_history(session, prov.name, limit=6, use_portal=False)
            overview.append((prov.name, hist["readings"]))
    return _format_meters_overview(overview)


async def _drafts_block(portal: list[InfolvivReading] | None = None) -> str | None:
    """Photo readings still in my pocket that the portal does NOT yet reflect.

    The portal block is authoritative; a local draft is only worth surfacing when it's
    genuinely ahead of it — i.e. the portal has no filed reading for that meter's month
    (or newer). A draft whose value/month is already on the portal is just noise («нам
    треба знати лише дані»), so it's dropped. A fresh photo supersedes the previous draft
    of the same meter (`_supersede_pending`), so at most one per meter survives anyway.
    """
    account_names, primary_name = await _household_naming()
    # Latest filed period per (kind, household) — what the portal already shows.
    filed: dict[tuple[str, str | None], str] = {}
    for pr in portal or []:
        if pr.period:
            key = (pr.kind, account_names.get(pr.account_code) or primary_name)
            if pr.period > filed.get(key, ""):
                filed[key] = pr.period

    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(MeterReading)
                    .where(
                        MeterReading.status.in_(
                            (MeterStatus.validated, MeterStatus.needs_confirm)
                        ),
                        MeterReading.value.is_not(None),
                    )
                    .order_by(MeterReading.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return None
        provs = {p.id: p for p in (await session.execute(select(Provider))).scalars()}
        hh_names = {
            h.id: h.name for h in (await session.execute(select(Household))).scalars()
        }
    # Defensive de-dup: freshest per meter (supersede already keeps it to one).
    freshest: dict[int, MeterReading] = {}
    for r in rows:
        if r.provider_id is not None and r.provider_id not in freshest:
            freshest[r.provider_id] = r
    lines = ["📝 Збережено з фото (ще не подано на портал):"]
    for pid, r in freshest.items():
        prov = provs.get(pid)
        # Name the property, like the portal block — so a draft never reads ambiguously.
        hh = (
            hh_names.get(prov.household_id)
            if prov and prov.household_id is not None
            else None
        )
        kind = prov.category.value if prov else ""
        # Already on the portal for this month (or newer)? → redundant, skip it.
        if r.cycle <= filed.get((kind, hh), ""):
            continue
        label = _KIND_LABEL.get(kind, prov.name if prov else "🔢 Лічильник")
        suffix = f" · {hh}" if hh else ""
        state = (
            "чекає підтвердження" if r.status is MeterStatus.needs_confirm else "записав"
        )
        lines.append(f"{html.escape(label + suffix)} — {r.value} ({state})")
    return "\n".join(lines) if len(lines) > 1 else None


@router.message(F.text == keyboards.MENU_METERS)
async def menu_meters(message: Message) -> None:
    # On a cold cache this authenticates against the infolviv portal and fetches — keep
    # «друкує…» up for the whole thing, so the button never looks like it did nothing.
    async with _thinking(message):
        try:
            readings = await fetch_infolviv_readings()
        except Exception:
            log.exception("infolviv fetch raised")
            readings = []
        if readings:
            # Portal record (authoritative) + any photo drafts I'm still holding.
            account_names, primary_name = await _household_naming()
            blocks = [_format_infolviv_readings(readings, account_names, primary_name)]
            drafts = await _drafts_block(readings)
            if drafts:
                blocks.append(drafts)
            text, parse_mode = "\n\n".join(blocks), "HTML"
        else:
            # Portal not configured / unreachable → show what I've saved from photos.
            text, parse_mode = await _local_journal(), None
    await message.answer(_trim(text), parse_mode=parse_mode)


# One button per row here, and Telegram limits a keyboard's total size — an unbounded
# list would eventually make the whole «📜 Історія» send fail. The newest photos are the
# ones anyone reaches for; older ones stay reachable by asking.
_PHOTO_BUTTONS = 8


def _journal_photo_buttons(sections: list[dict]) -> list[tuple[int, str]]:
    """One «📸 Фото» button per journal reading that still has an archived photo —
    label names the meter + month so a tap pulls back exactly that month's image.

    Capped at the newest `_PHOTO_BUTTONS` ACROSS ALL METERS. The cap has to sort by month
    first: taking it in provider order meant that once gas had 8 photos, water's buttons
    vanished entirely — including this month's — while eight old gas ones stayed.
    """
    items: list[tuple[str, int, str]] = []
    for sec in sections:
        for r in sec["readings"]:
            # photo_id is the reading whose archived file actually survives (may differ
            # from the displayed row), so the tap always lands on a real photo.
            if r.get("has_photo") and r.get("photo_id") is not None:
                label = f"📸 {sec['provider']} · {_format_cycle(r['cycle'])}"
                items.append((str(r["cycle"]), r["photo_id"], label))
    items.sort(key=lambda it: it[0], reverse=True)  # newest month first, any meter
    return [(pid, label) for _cycle, pid, label in items[:_PHOTO_BUTTONS]]


@router.message(F.text == keyboards.MENU_HISTORY)
async def menu_history(message: Message) -> None:
    """«📜 Історія» — a small chooser (readings / payments) so neither view dumps the
    whole timeline at once. Each leaf opens in place and carries a «⬅️ Назад» button."""
    await message.answer(
        "📜 Що показати?", reply_markup=keyboards.history_menu_keyboard()
    )


async def _households_for_nav(session) -> list[tuple[str, str]]:
    return [
        (h.slug, h.name)
        for h in (
            await session.execute(select(Household).order_by(Household.is_primary.desc()))
        ).scalars()
    ]


@router.callback_query(F.data.startswith("h:"))
async def on_history_nav(callback: CallbackQuery) -> None:
    """«📜 Історія» navigation — edits the message in place between the root menu, the
    readings journal, and payments (split per household when there are two)."""
    if not callback.data:
        await callback.answer()
        return
    parts = callback.data.split(":")
    view = parts[1] if len(parts) > 1 else "menu"
    arg = parts[2] if len(parts) > 2 else None

    if view == "menu":
        await _edit(callback, "📜 Що показати?", keyboards.history_menu_keyboard())
        await callback.answer()
        return

    if view == "met":
        async with session_scope() as session:
            result = await get_meter_journal(session)
        photo_items = _journal_photo_buttons(result["sections"])
        await _edit(
            callback, result["message"], keyboards.history_meters_keyboard(photo_items)
        )
        await callback.answer()
        return

    if view == "pay":
        async with session_scope() as session:
            households_list = await _households_for_nav(session)
            multi = len(households_list) > 1
            # Two properties → choose one first (the combined list is very long).
            if arg is None and multi:
                await _edit(
                    callback,
                    "💸 Платежі за яким житлом?",
                    keyboards.history_households_keyboard(households_list),
                )
                await callback.answer()
                return
            result = await get_payment_journal(session, household=arg)
        back = "h:pay" if (arg is not None and multi) else "h:menu"
        await _edit(callback, result["message"], keyboards.history_back_keyboard(back))
        await callback.answer()
        return

    await callback.answer()


@router.message(F.text == keyboards.MENU_PAYPLAN)
async def menu_payplan(message: Message) -> None:
    """«🗓 Як платити» — the monthly payment plan: per service the due day, typical amount
    and through which service it's paid, plus tappable pay links (monobank / ДАХ /
    Portmone)."""
    async with session_scope() as session:
        result = await get_payment_plan(session)
    markup = keyboards.links_keyboard(result.get("links") or [])
    await message.answer(result["message"], reply_markup=markup)


@router.callback_query(F.data.startswith("mp:"))
async def on_meter_photo(callback: CallbackQuery) -> None:
    """«📸 Фото» tap → send that specific reading's archived photo with its caption."""
    if not callback.data:
        await callback.answer()
        return
    try:
        reading_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    async with session_scope() as session:
        res = await get_meter_photo_by_id(session, reading_id)
    if isinstance(callback.message, Message):
        photo_path = res.get("photo_path")
        if res.get("ok") and photo_path and os.path.exists(photo_path):
            await callback.message.answer_photo(
                FSInputFile(photo_path),
                caption=res.get("caption_html") or res.get("caption"),
                parse_mode="HTML",
            )
        else:
            await callback.message.answer(res.get("message") or "Фото не знайшов.")
    await callback.answer()


@asynccontextmanager
async def _thinking(message: Message, action: str = "typing") -> AsyncIterator[None]:
    """Show a Telegram chat action while we work, so a slow LLM/vision turn never looks
    frozen. Default «друкує…» (typing); pass `action="record_voice"` for «записує аудіо…»
    on a voice turn. No-ops if the bot/chat isn't real (e.g. in tests). Body exceptions
    propagate to the caller's own error handling — we only gate the indicator itself."""
    bot = getattr(message, "bot", None)
    chat = getattr(message, "chat", None)
    if not isinstance(bot, Bot) or chat is None:
        yield
        return
    async with ChatActionSender(bot=bot, chat_id=chat.id, action=action):
        yield


# Rolling free-text dialogue PER CHAT, so the agent can resolve short replies («давай»,
# «а за травень?») against its own previous line in THAT conversation.
#
# Per chat, not per process: the allowlist admits the owner AND family, and one shared
# deque meant a family member's two messages evicted the owner's context entirely — then
# the owner's «давай» resolved against whatever someone else had just asked. The allowlist
# is tiny, so this is capped only so a stray id can't grow it without bound.
_DIALOGUE_TURNS = 6
_DIALOGUE_CHATS = 16
_DIALOGUE: OrderedDict[int, deque[dict[str, str]]] = OrderedDict()


def _dialogue(chat_id: int) -> deque[dict[str, str]]:
    hist = _DIALOGUE.get(chat_id)
    if hist is None:
        hist = deque(maxlen=_DIALOGUE_TURNS)
        _DIALOGUE[chat_id] = hist
        while len(_DIALOGUE) > _DIALOGUE_CHATS:
            _DIALOGUE.popitem(last=False)  # evict the least-recently-used chat
    _DIALOGUE.move_to_end(chat_id)
    return hist


async def _try_voice(
    message: Message,
    ogg_path: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Send a synthesized voice note. Returns True if delivered, False if Telegram refused
    it — most often `VOICE_MESSAGES_FORBIDDEN` (the recipient's privacy setting forbids
    voice messages from bots), but any send error returns False so the caller falls back
    to text and the answer still lands. Stateless: the moment the recipient allows voice
    notes, the next turn delivers one."""
    try:
        await message.answer_voice(FSInputFile(ogg_path), reply_markup=reply_markup)
        return True
    except Exception as exc:
        log.warning("voice note not delivered (%s) — replying in text", exc)
        return False


async def _respond_to_text(
    message: Message, user_text: str, *, voice_reply: bool = False
) -> None:
    """Run one free-text turn through the agent and render the reply (buttons + chart).
    Shared by the text and voice handlers. Before the tool runs the agent sends a short,
    natural «I'm on it» line («зазираю в кабінет інтернету…») — for both typed and voiced
    asks — so the bot feels like a real assistant rather than echoing the request back.

    `voice_reply` (set by the voice handler) makes the bot answer in a **voice note**: the
    chat header shows «записує аудіо…» (not «друкує…»), the reply is synthesized locally
    (Piper) and sent as audio, with any image (chart/photo) still attached. If synth or
    the voice send fails (e.g. the recipient forbids voice messages) it falls back to a
    text reply, so a voice asker is never left empty-handed."""

    async def _say_progress(line: str) -> None:
        # A voice turn already signals work via the «записує аудіо…» header — don't also
        # post a text «I'm on it» line (it reads as a stray text bubble before the voice
        # reply). Still wired (not None) so the dispatcher composes the answer as just the
        # data, with no «зараз гляну» preamble in what we then synthesize.
        if not voice_reply:
            await message.answer(line)

    # A voice turn signals «записує аудіо…»; a typed/photo turn «друкує…».
    work_action = "record_voice" if voice_reply else "typing"
    chat = getattr(message, "chat", None)
    history = _dialogue(chat.id) if chat is not None else deque(maxlen=_DIALOGUE_TURNS)
    try:
        async with _thinking(message, work_action), session_scope() as session:
            reply = await agent_dispatcher.handle_message(
                user_text,
                session,
                get_provider(),
                history=list(history),
                on_progress=_say_progress,
            )
    except Exception:
        # Anything from context-building, the LLM path, or the DB lands here.
        # Log the traceback (otherwise it's swallowed → silent Telegram) and still
        # reply, so the user never faces dead air.
        log.exception("agent turn failed for %r", user_text)
        await message.answer(_OOPS)
        return
    # Record the turn so the next message has this exchange as context.
    history.append({"role": "user", "text": user_text})
    history.append({"role": "assistant", "text": reply.text or ""})

    # Voice in → voice out: synthesize the spoken reply locally (still showing «записує
    # аудіо…»). None (synth disabled, no model, too long, or an error) → send text, so the
    # user always gets an answer.
    voice_ogg: str | None = None
    if voice_reply:
        try:
            async with _thinking(message, "record_voice"):
                voice_ogg = await get_tts_provider().synthesize(reply.text or "")
        except Exception:
            log.exception("tts synth raised; replying in text")

    try:
        markup = None
        tr = reply.tool_result or {}
        # A retrieved meter photo: send the image with its caption, not a text line.
        photo_path = tr.get("photo_path")
        if photo_path and os.path.exists(photo_path):
            # HTML caption (value in <code>) so Telegram doesn't auto-link the digit run.
            await message.answer_photo(
                FSInputFile(photo_path),
                caption=tr.get("caption_html") or tr.get("caption") or reply.text,
                parse_mode="HTML",
            )
            if voice_ogg:
                await _try_voice(
                    message, voice_ogg
                )  # best-effort; photo already answered
            return
        if tr.get("pay_link"):
            markup = keyboards.pay_keyboard(tr["pay_link"], label=tr.get("pay_label"))
        elif tr.get("links"):  # payment plan → one pay button per distinct service
            markup = keyboards.links_keyboard(tr["links"])
        elif tr.get("confirm_delete"):
            markup = keyboards.meter_delete_confirm_keyboard(tr["confirm_scope"])
        # The voice note is the reply (buttons ride on it). If Telegram refuses it (e.g.
        # recipient forbids voice messages), fall back to the text reply — never dead air.
        if not (voice_ogg and await _try_voice(message, voice_ogg, reply_markup=markup)):
            await message.answer(_trim(reply.text or "…"), reply_markup=markup)
        if reply.chart_path:
            await _send_chart(message, reply.chart_path)
    finally:
        if voice_ogg and os.path.exists(voice_ogg):
            os.unlink(voice_ogg)  # transient — never linger on disk


@router.message(F.text)
async def on_text(message: Message) -> None:
    await _respond_to_text(message, message.text or "")


_OOPS = "Щось у моїх паперах заклинило — спробуйте ще раз за мить."

# Telegram hard-rejects a message over 4096 chars and a photo caption over 1024 — with
# TelegramBadRequest, not a truncation. A growing journal reaches these on its own, and a
# rejected edit meant the view was simply broken from then on.
_MAX_TEXT = 4096
_MAX_CAPTION = 1024
_ELLIPSIS = "\n…"


def _trim(text: str, limit: int = _MAX_TEXT) -> str:
    """Cut `text` to `limit` chars, on a line boundary.

    Line boundaries are not just cosmetic: some of these messages carry HTML (`<code>`
    around an account number), and every tag opens and closes within one line — so
    cutting at a newline can never split a tag. A mid-tag cut would make Telegram reject
    the whole message with «can't parse entities», which is worse than the length problem
    we're solving. With no newline to cut at, fall back to the last '<' so a partial tag
    still can't survive.
    """
    if len(text) <= limit:
        return text
    head = text[: limit - len(_ELLIPSIS)]
    cut = head.rfind("\n")
    if cut <= 0:
        tag = head.rfind("<")
        cut = tag if tag > 0 else len(head)
    return head[:cut].rstrip() + _ELLIPSIS


def _cap(text: str | None) -> str | None:
    """Trim to the photo-caption limit (1024) — a quarter of the text limit."""
    return _trim(text, _MAX_CAPTION) if text else text


_CHART_PREFIX = "dvoretskyi_stats_"


def _drop_chart(path: str | None) -> None:
    """Delete a rendered chart once it's been sent.

    Charts are one-shot: Telegram has its own copy the moment the upload completes, so a
    PNG left behind is pure litter (production had 68 of them, 4.7MB, going back a month
    — nothing ever reaped them). Guarded to our own temp prefix, the way
    `photo_store.remove` guards to the archive dir, so a stray path can never make this
    delete something that matters.
    """
    if not path or not os.path.basename(path).startswith(_CHART_PREFIX):
        return
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError as exc:  # a leaked temp file must never cost the user their reply
        log.warning("could not remove chart %s: %s", path, exc)


async def _send_chart(
    message: Message,
    path: str,
    *,
    caption: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Send a chart PNG and delete it, whatever happens to the send."""
    try:
        await message.answer_photo(
            FSInputFile(path), caption=_cap(caption), reply_markup=reply_markup
        )
    finally:
        _drop_chart(path)


async def _replace_stats(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
    result: dict,
) -> None:
    """Swap a stats view in place — new chart, new caption, new buttons, same message.

    Editing beats sending: tapping ◀/▶ through a year should leave ONE chart on screen,
    not twelve. Falls back to a fresh message when the original can't be edited (e.g. a
    text-only view being replaced by a chart, or a message too old to edit).
    """
    path = result.get("chart_path")
    msg = callback.message
    if not isinstance(msg, Message):
        _drop_chart(path)
        return
    try:
        if path and msg.photo:
            await msg.edit_media(
                InputMediaPhoto(media=FSInputFile(path), caption=_cap(text)),
                reply_markup=reply_markup,
            )
        elif path:  # a text message can't become a photo by editing — send a new one
            await _send_chart(msg, path, caption=text, reply_markup=reply_markup)
            return
        elif msg.photo:
            # The new view has NO chart (an empty month, or a render that failed) while a
            # chart is on screen. Editing only the caption would leave the PREVIOUS
            # month's chart above text about a different month — a picture that lies.
            # Drop the message and send the text fresh instead.
            with suppress(TelegramBadRequest):
                await msg.delete()
            await msg.answer(_trim(text), reply_markup=reply_markup)
        else:
            await msg.edit_text(_trim(text), reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        # «message is not modified» = the user re-tapped the view they're already on.
        # Nothing to do, and certainly not an error worth showing them.
        if "not modified" not in str(exc).casefold():
            log.warning("stats edit failed (%s) — sending a fresh view", exc)
            await msg.answer(_trim(text), reply_markup=reply_markup)
    finally:
        _drop_chart(path)


async def _edit_caption(
    callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Swap just the caption/buttons of the current view (the chooser menus), keeping
    whatever image is already there."""
    msg = callback.message
    if not isinstance(msg, Message):
        return
    try:
        if msg.photo:
            await msg.edit_caption(caption=_cap(text), reply_markup=reply_markup)
        else:
            await msg.edit_text(_trim(text), reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc).casefold():
            log.warning("caption edit failed: %s", exc)


async def _edit(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit the originating message if it's still accessible; else send a fresh one."""
    if isinstance(callback.message, Message):
        await callback.message.edit_text(_trim(text), reply_markup=reply_markup)
    elif callback.bot is not None and callback.message is not None:
        await callback.bot.send_message(
            callback.message.chat.id, _trim(text), reply_markup=reply_markup
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
            hh = (
                await session.get(Household, provider.household_id)
                if provider.household_id is not None
                else None
            )
            result = await categorize_payment(
                session,
                payment.mono_tx_id,
                provider.name,
                household=hh.slug if hh else None,
            )
            suffix = await _household_suffix(session, provider)
            # «запам'ятав» only when a pattern was actually learned (a distinctive payee
            # token, or the особовий рахунок for a shared utility) — else just «записав».
            tail = "записав і запам'ятав" if result.get("learned_pattern") else "записав"
            text = f"✅ {result['provider']}{suffix} — {result['amount_uah']} ₴, {tail}."

    await _edit(callback, text)
    await callback.answer()


@router.callback_query(F.data.startswith("ch:"))
async def on_correct_household(callback: CallbackQuery) -> None:
    """«↪ Це <інше житло>» — move an auto-logged shared payment to the other property."""
    if not callback.data:
        await callback.answer()
        return
    _, pid = callback.data.split(":", 1)
    async with session_scope() as session:
        payment = await session.get(Payment, int(pid))
        if payment is None or payment.provider_id is None:
            await callback.answer("Не знайшов платіж.")
            return
        cur = await session.get(Provider, payment.provider_id)
        alt = await _other_household_provider(session, cur)
        if alt is None:
            await callback.answer("Нема куди переносити.")
            return
        payment.provider_id = alt.id
        alt_hh = await session.get(Household, alt.household_id)
        suffix = f" · {alt_hh.name}" if alt_hh and alt_hh.name else ""
        text = f"✅ {alt.name}{suffix} — {payment.amount_uah} ₴ (перенесено)."
    await _edit(callback, text)
    await callback.answer("Перенесено ✓")


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


async def _download_voice(message: Message) -> str:
    """Download a voice note (OGG/Opus) to the private media dir. Injectable in tests."""
    if not message.voice or message.bot is None:
        raise RuntimeError("no voice to download")
    voice = message.voice
    _MEDIA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _MEDIA_DIR / f"{voice.file_unique_id}.ogg"
    await message.bot.download(voice, destination=str(path))
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


async def _other_household_provider(
    session, provider: Provider | None
) -> Provider | None:
    """The same-named provider in the OTHER household (ЛЕЗ, Газ доставлення) — the target
    of the «↪ Це <інше житло>» correction. None for a provider unique to one property."""
    if provider is None or provider.household_id is None:
        return None
    rows = (
        (
            await session.execute(
                select(Provider).where(
                    Provider.name == provider.name, Provider.id != provider.id
                )
            )
        )
        .scalars()
        .all()
    )
    return next((r for r in rows if r.household_id != provider.household_id), None)


async def _household_suffix(session, provider: Provider | None) -> str:
    """« · <житло>» on every confirmation so it's always clear WHICH property it was filed
    under (the user pays for two homes and wants no ambiguity). **Mobile is exempt**: a
    phone top-up isn't tied to a property, so naming a household there is just noise."""
    if provider is None or provider.household_id is None:
        return ""
    if provider.category is Category.mobile:
        return ""
    hh = await session.get(Household, provider.household_id)
    return f" · {hh.name}" if hh and hh.name else ""


# Phone numbers / особові рахунки — long digit runs that aren't a payee name.
_PAYEE_NOISE = re.compile(r"\+?\d{6,}")


def _payee_hint(raw: str | None) -> str:
    """A short, human payee label for an «unknown payment» prompt: collapse monobank's
    newline-joined fields onto one line and drop long digit runs (phone numbers, account
    codes) so the user sees «Lifecell», not «Lifecell +380…». '' when nothing readable."""
    if not raw:
        return ""
    cleaned = " ".join(_PAYEE_NOISE.sub(" ", raw).split())
    return cleaned.strip(" -·,")


async def _meter_providers(session) -> list[Provider]:
    """Photo meters live in the **primary** household (the home). The secondary property
    is unoccupied → its meter is a static value filed without a photo (Phase D), so it's
    excluded here: a photo the user sends is always for home."""
    prim = await households.primary(session)
    stmt = select(Provider).where(Provider.meter_window.is_not(None))
    if prim is not None:
        stmt = stmt.where(Provider.household_id == prim.id)
    return list((await session.execute(stmt.order_by(Provider.id))).scalars().all())


def _stored_line(result: dict) -> str:
    """«💧 Холодна вода: записав X (+спожито).» — names the meter + value saved."""
    label = _KIND_LABEL.get(result.get("kind") or "", "🔢 Лічильник")
    value = result.get("value")
    cons = result.get("consumption")
    extra = f" (намотало +{cons})" if cons not in (None, "None") else ""
    return f"{label}: записав {value}{extra}."


def _gated_meter_reply(
    result: dict, now: datetime | None = None
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Reply for a freshly-read reading, gated by the calendar.

    needs_confirm → confirm/re-photo. validated → store it and offer the date-gated
    action: inside the 28+ window an approve button (one tap files it); before it, a
    «подай раніше» button that resists twice and files on the 3rd tap."""
    rid = result.get("reading_id")
    status = result.get("status")
    label = _KIND_LABEL.get(result.get("kind") or "", "🔢 Лічильник")
    value = result.get("value")
    if status == MeterStatus.needs_confirm.value and rid is not None:
        # Always name the meter + the number I read, then the doubt — so it's never a
        # bare «Нуль споживання…» with no idea which meter or value.
        head = f"{label}: бачу {value}." if value else f"{label}:"
        reason = result.get("message") or ""
        return f"{head}\n{reason}".strip(), keyboards.meter_confirm_delete_keyboard(rid)
    if status != MeterStatus.validated.value or rid is None:
        return result.get("message") or "…", None

    now = now or clock.now()
    window = _submission_window_label(now)
    stored = _stored_line(result)
    if now.day >= get_settings().meter_submit_from_day:
        text = f"{stored}\n🗓 Вікно подачі відкрите ({window}). Подати на портал?"
        return text, keyboards.meter_approve_keyboard(rid)
    # NOT «подам» — nothing files a reading without the user's tap, by design. Promising
    # to file it was a lie the bot couldn't keep: the draft sat unfiled and unmentioned.
    # We promise the reminder we actually send (engine.compute_pending_meter_nudges
    # raises this draft again once the window opens, with the one-tap approve button).
    text = (
        f"{stored}\n🗓 Нагадаю у вікні {window} — подаси одним дотиком; наприкінці "
        "місяця показник найактуальніший.\nЯкщо дуже треба раніше — тисни нижче."
    )
    return text, keyboards.meter_early_keyboard(rid, 1)


# Butler-voice pushback for the 1st/2nd «подай раніше» tap (the 3rd actually files).
_EARLY_PUSHBACK = (
    "Ще рано — до {window} показник може ще «підрости», тоді подамо найсвіжіший. "
    "Точно подати зараз?",
    "Я б усе ж зачекав до кінця місяця. Але як наполягаєш — тисни ще раз, і подаю.",
)


async def _file_reading(rid: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """Submit a stored reading to infolviv. On success → submitted; if the live POST is
    disabled (body unverified) → fall back to manual filing + the «Відправив ✓» tap."""
    async with session_scope() as session:
        reading = await session.get(MeterReading, rid)
        if reading is None or reading.value is None or reading.provider_id is None:
            return "Показник зник — надішли фото ще раз.", None
        provider = await session.get(Provider, reading.provider_id)
        if reading.status is MeterStatus.submitted:
            # Already filed. A second tap is ordinary impatience — the POST is a live
            # HTTP call, so the button looks unresponsive for a second. Without this the
            # second call re-POSTed, the portal refused it («value not greater»), and the
            # error branch below reset a SUCCESSFULLY filed reading back to `validated`
            # — leaving it an outstanding draft that nudges forever.
            meter_name = provider.name if provider else "показник"
            return f"Уже подав — {meter_name}: {reading.value}.", None
        kind = provider.category.value if provider else ""
        # Name the meter in every reply — «Подав: 107.695» alone left the user guessing
        # whether it was gas or water (a photo turn can file either).
        meter = provider.name if provider else "показник"
        # Route to the right counter by household (two properties share one infolviv
        # login → the account code disambiguates; None = first matching kind).
        account = None
        if provider is not None and provider.household_id is not None:
            hh = await session.get(Household, provider.household_id)
            account = hh.infolviv_account_code if hh else None
        value = reading.value
        try:
            await submit_infolviv_reading(kind, value, account_code=account)
        except InfolvivSubmitDisabled:
            # Live POST not enabled → hand back the value for manual filing.
            reading.status = MeterStatus.validated
            await session.flush()
            text = (
                f"Підготував до подачі — {meter}: {value}. Подай на порталі infolviv "
                "і тисни «Відправив ✓»."
            )
            return text, keyboards.meter_submitted_keyboard(rid)
        except InfolvivSubmitError as exc:
            # The portal refused it (e.g. value below the current one) — show its reason
            # and keep the reading stored so a corrected re-photo replaces it.
            reading.status = MeterStatus.validated
            await session.flush()
            return f"⚠️ infolviv не прийняв {meter}: {exc}", None
        except Exception:
            log.exception("infolviv submit failed")
            return f"Не вдалося подати {meter} на портал — спробуй ще раз за мить.", None
        reading.status = MeterStatus.submitted
        reading.submitted_at = clock.now()
        await session.flush()
    return f"✅ Подав на infolviv — {meter}: {value}.", None


@router.message(F.photo)
async def on_photo(message: Message) -> None:
    path: str | None = None
    try:
        # Keep «друкує…» up across the WHOLE turn (download + the single OCR pass +
        # store), not just OCR — else the indicator vanishes and the reply looks frozen.
        async with _thinking(message):
            path = await _download_photo(message)
            async with session_scope() as session:
                meter_provs = await _meter_providers(session)
                if not meter_provs:
                    await message.answer("Лічильників у списку нема — нема куди вносити.")
                    return

                # ONE anchored vision pass decides everything: kind (dark→water,
                # light→gas, else "other") AND value, with each meter's previous reading
                # as a hint so an ambiguous wheel (108 vs 148) reads true. One round —
                # no blind read followed by a hinted re-read — so the wait is a single
                # vision call, not two.
                hints = await meter_hints(session, meter_provs)
                read = await get_vision_provider().read_meter(path, hints=hints)
                if read.kind == "other":
                    # Not a meter: react with a light remark, store nothing.
                    await message.answer(
                        read.comment
                        or "Гарне фото, але лічильника на ньому я не бачу. 🎩"
                    )
                    return

                # A caption can still override; otherwise route by the detected kind.
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
                        reply_markup=keyboards.meter_route_keyboard(
                            reading.id, meter_provs
                        ),
                    )
                    path = None  # keep the file — the m: callback OCRs then deletes it
                    return

                result = await submit_meter_reading(
                    session,
                    chosen.name,
                    path,
                    read=read,  # already OCR'd (anchored) — submit won't re-read
                    auto_submit=False,  # store now; file via the date-gated flow
                )
            text, kb = _gated_meter_reply(result)
            await message.answer(text, reply_markup=kb)
    except Exception:
        log.exception("on_photo failed")
        await message.answer("Не зміг обробити фото — спробуй ще раз за мить.")
    finally:
        if path and os.path.exists(path):
            os.unlink(path)


@router.message(F.voice)
async def on_voice(message: Message) -> None:
    """Voice note → transcribe locally → run it as a normal agent turn → answer by voice.
    The audio file is deleted right after; bytes are never logged. Meter *values* still
    come only from photos (STT misreads digits), so a voice turn can ask or act but never
    files a reading — and destructive actions keep their existing confirm-tap. The reply
    is spoken back (local Piper TTS); if synth is disabled/unavailable it falls back to
    text, so a voice asker is never left without an answer."""
    if get_settings().stt_provider.casefold() == "none":
        await message.answer("Голосові я поки не слухаю — напиши, будь ласка, текстом.")
        return
    path: str | None = None
    transcript = ""
    try:
        # «записує аудіо…» from the very first second (download + transcription), so the
        # whole voice turn reads as one action — no «друкує…» flicker before the reply.
        async with _thinking(message, "record_voice"):
            path = await _download_voice(message)
            transcript = await get_transcription_provider().transcribe(path)
    except Exception:
        log.exception("on_voice download/transcribe failed")
    finally:
        if path and os.path.exists(path):
            os.unlink(path)  # audio is transient — never linger on disk
    transcript = transcript.strip()
    if not transcript:
        await message.answer("Не розчув голосове — спробуй ще раз або напиши текстом.")
        return

    # No verbatim echo — `_respond_to_text` sends a natural «I'm on it» line once the
    # agent knows what to do (same as for typed asks). Voice in → voice out: the answer
    # comes back as a spoken note (with a text fallback if synth is off/unavailable).
    await _respond_to_text(message, transcript, voice_reply=True)


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
            auto_submit=False,
        )
    text, kb = _gated_meter_reply(result)
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
            result = await confirm_meter_reading(session, reading.id, auto_submit=False)
        except ToolError as exc:
            await callback.answer(str(exc))
            return
    text, kb = _gated_meter_reply(result)
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


@router.callback_query(F.data.startswith("sf:"))
async def on_meter_approve(callback: CallbackQuery) -> None:
    """In-window approval («📤 Подати на портал») → file the reading now."""
    if not callback.data:
        await callback.answer()
        return
    _, rid_s = callback.data.split(":", 1)
    text, kb = await _file_reading(int(rid_s))
    await _edit(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("mdc:"))
async def on_meter_delete_confirm(callback: CallbackQuery) -> None:
    """Confirm/cancel a bulk delete asked for in chat («видали всі показники»)."""
    if not callback.data:
        await callback.answer()
        return
    _, scope = callback.data.split(":", 1)
    if scope == "no":
        await _edit(
            callback,
            random.choice(
                (
                    "Гаразд, лишив усе як є.",
                    "Ок, нічого не чіпаю.",
                    "Добре, скасував.",
                    "Як скажете — нічого не видаляю.",
                    "Зрозумів, залишаю все на місці.",
                    "Гаразд, відбій. Показники цілі.",
                )
            ),
        )
        await callback.answer()
        return
    async with session_scope() as session:
        result = await execute_meter_delete(session, scope)
    await _edit(callback, result["message"])
    await callback.answer()


@router.callback_query(F.data.startswith("md:"))
async def on_meter_delete(callback: CallbackQuery) -> None:
    """«🗑 Видалити» → drop the stored reading (wrong value entered)."""
    if not callback.data:
        await callback.answer()
        return
    _, rid_s = callback.data.split(":", 1)
    async with session_scope() as session:
        try:
            result = await delete_meter_reading(session, reading_id=rid_s)
        except ToolError as exc:
            await callback.answer(str(exc))
            return
    await _edit(callback, result["message"])
    await callback.answer()


@router.callback_query(F.data.startswith("se:"))
async def on_meter_early(callback: CallbackQuery) -> None:
    """«⏳ Подати раніше» before the 28th: resist twice, file on the 3rd insistence."""
    if not callback.data:
        await callback.answer()
        return
    _, rid_s, attempt_s = callback.data.split(":", 2)
    rid, attempt = int(rid_s), int(attempt_s)
    now = clock.now()
    settings = get_settings()
    if meters.submit_now(
        now,
        attempt=attempt,
        submit_from_day=settings.meter_submit_from_day,
        max_attempts=settings.meter_early_submit_attempts,
    ):
        text, kb = await _file_reading(rid)
        await _edit(callback, text, kb)
    else:
        # Resist; the next tap carries the bumped attempt count.
        pushback = _EARLY_PUSHBACK[min(attempt - 1, len(_EARLY_PUSHBACK) - 1)]
        await _edit(
            callback,
            pushback.format(window=_submission_window_label(now)),
            keyboards.meter_early_keyboard(rid, attempt + 1),
        )
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


async def on_error(event: ErrorEvent) -> bool:
    """Last line of defence: never leave a tap unanswered.

    Only `_respond_to_text` had its own try/except, so a raise anywhere else (a chart
    failure, a portal timeout, a TelegramBadRequest) was swallowed by aiogram's logger:
    the user tapped 📊 / 🗓 / 📜 and got NOTHING back — and on a callback the tap spinner
    kept turning for ~30s because `callback.answer()` was never reached. Here we log the
    traceback and always close the loop with one honest line.
    """
    log.exception("unhandled error in update: %s", event.exception)
    callback = event.update.callback_query
    message = event.update.message
    with suppress(Exception):  # the fallback must never raise a second time
        if callback is not None:
            await callback.answer(_OOPS, show_alert=False)
        elif message is not None:
            await message.answer(_OOPS)
    return True  # handled — don't re-raise into the polling loop


def build_dispatcher() -> Dispatcher:
    settings = get_settings()
    dp = Dispatcher()
    dp.update.outer_middleware(AllowlistMiddleware(settings.allowed_user_ids))
    dp.include_router(router)
    dp.errors.register(on_error)
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
            markup = None
            async with session_scope() as session:
                prov = (
                    await session.get(Provider, notice.provider_id)
                    if notice.provider_id is not None
                    else None
                )
                suffix = await _household_suffix(session, prov)
                # Shared utility auto-logged to the default home → offer one tap to
                # move it to the other property (the rare secondary payment).
                alt = await _other_household_provider(session, prov)
                if alt is not None and notice.payment_id is not None:
                    alt_hh = await session.get(Household, alt.household_id)
                    label = (
                        f"↪ Це {alt_hh.name}"
                        if alt_hh and alt_hh.name
                        else "↪ Інше житло"
                    )
                    markup = keyboards.correct_household_keyboard(
                        notice.payment_id, label
                    )
            await bot.send_message(
                chat_id,
                f"✅ {notice.provider_name}{suffix} — {notice.amount_uah} ₴, записав.",
                reply_markup=markup,
            )
        elif notice.action is Action.UNCATEGORIZED and notice.payment_id is not None:
            async with session_scope() as session:
                providers = (
                    (await session.execute(select(Provider).order_by(Provider.id)))
                    .scalars()
                    .all()
                )
                hh_names = {
                    h.id: h.name
                    for h in (await session.execute(select(Household))).scalars()
                }
            kb = keyboards.categorize_keyboard(notice.payment_id, providers, hh_names)
            hint = _payee_hint(notice.raw_description)
            lead = f" від «{hint}»" if hint else ""
            await bot.send_message(
                chat_id,
                f"Прилетіло {notice.amount_uah} ₴{lead}, а такого в мене нема. Це що?",
                reply_markup=kb,
            )

    return notify
