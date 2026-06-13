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

from komunalka.bot.app import build_bot, build_dispatcher, make_notifier
from komunalka.mono.webhook import router as mono_router
from komunalka.reminders.engine import build_scheduler, schedule_jobs

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    bot = build_bot()
    dp = build_dispatcher()

    # Webhook → Telegram notifier.
    app.state.bot = bot
    app.state.notifier = make_notifier(bot)

    # Reminders scheduler.
    scheduler = build_scheduler()

    async def _send(chat_id: int, text: str) -> None:
        await bot.send_message(chat_id, text)

    schedule_jobs(scheduler, _send)
    scheduler.start()
    app.state.scheduler = scheduler

    # Telegram long polling as a background task.
    polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    log.info("komunalka started: webhook + polling + scheduler")

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
        log.info("komunalka stopped")


def create_app() -> FastAPI:
    app = FastAPI(title="Komunalka — Комунальний Дворецький", lifespan=lifespan)
    app.include_router(mono_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
