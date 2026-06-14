"""FastAPI application.

Lifespan starts: (1) the mono webhook router, (2) Telegram long-polling, (3) the
reminders scheduler. The Bot is shared via app.state so the webhook can push messages.
Single-user, so long polling is fine — no public Telegram endpoint needed.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from dvoretskyi.bot import keyboards
from dvoretskyi.bot.app import (
    build_bot,
    build_dispatcher,
    make_notifier,
    set_my_commands,
)
from dvoretskyi.mono.webhook import router as mono_router
from dvoretskyi.reminders.engine import build_scheduler, schedule_jobs

log = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Send `dvoretskyi.*` INFO logs to stderr (→ journald under systemd).

    uvicorn configures only its own loggers; without this our app loggers propagate to
    the root logger (level WARNING, last-resort handler), so INFO lines — e.g. the
    per-tx `mono tx: …` visibility log — are silently dropped. Idempotent; `propagate`
    is disabled so records aren't also emitted by the root handler (no duplicates).
    """
    logger = logging.getLogger("dvoretskyi")
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    bot = build_bot()
    dp = build_dispatcher()

    # Webhook → Telegram notifier.
    app.state.bot = bot
    app.state.notifier = make_notifier(bot)

    # Reminders scheduler.
    scheduler = build_scheduler()

    async def _send(
        chat_id: int,
        text: str,
        pay_link: str | None = None,
        pay_label: str | None = None,
    ) -> None:
        markup = keyboards.pay_keyboard(pay_link, label=pay_label) if pay_link else None
        await bot.send_message(chat_id, text, reply_markup=markup)

    schedule_jobs(scheduler, _send)
    scheduler.start()
    app.state.scheduler = scheduler

    # Publish the command menu so it lives in code, not just BotFather.
    try:
        await set_my_commands(bot)
    except Exception:  # noqa: BLE001 — non-fatal; menu is cosmetic
        log.warning("could not set bot commands", exc_info=True)

    # Telegram long polling as a background task.
    polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    log.info("dvoretskyi started: webhook + polling + scheduler")

    try:
        yield
    finally:
        polling.cancel()
        try:
            await polling
        except asyncio.CancelledError:
            pass
        scheduler.shutdown(wait=False)
        await bot.session.close()
        log.info("dvoretskyi stopped")


def create_app() -> FastAPI:
    app = FastAPI(title="Комунальний Дворецький", lifespan=lifespan)
    app.include_router(mono_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
