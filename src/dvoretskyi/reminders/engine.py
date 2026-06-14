"""APScheduler payment reminders.

Daily job: for each provider with a due_day, nudge iff this cycle is unpaid AND we're
inside the nudge window (due_day-3 … due_day) AND not snoozed AND not already nudged
today. Near the deadline (due_day or due_day-1) the copy escalates. A second daily job
nudges for meter readings (kind="meter") inside each meter's submission window.

Redis is used for the jobstore when reachable; otherwise it falls back to in-memory
(fine for single-user). Tests call `run_payment_nudges` directly — no scheduler needed.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dvoretskyi import clock
from dvoretskyi.agent import meters
from dvoretskyi.config import get_settings
from dvoretskyi.db.models import (
    Category,
    MeterReading,
    MeterStatus,
    NudgeKind,
    NudgeLog,
    Payment,
    Provider,
)
from dvoretskyi.db.session import session_scope

log = logging.getLogger(__name__)

NUDGE_WINDOW_DAYS = 3  # start nudging this many days before due_day


@dataclass
class PendingNudge:
    provider_id: int
    provider_name: str
    due_day: int
    expected_amount: str | None
    near_deadline: bool

    def message(self) -> str:
        amount = f" (≈{self.expected_amount} ₴)" if self.expected_amount else ""
        if self.near_deadline:
            return (
                f"{self.provider_name}{amount} — лишився останній день. "
                "Далі нарахують по-своєму, а ми цього не любимо. Платимо?"
            )
        return (
            f"Без фанатизму: {self.provider_name}{amount} цього місяця ще відкрите. "
            "Дедлайн не горить — просто щоб не загубилось."
        )


async def _paid_this_cycle(session: AsyncSession, provider_id: int, cycle: str) -> bool:
    year, month = (int(p) for p in cycle.split("-"))
    start = datetime(year, month, 1, tzinfo=clock.KYIV)
    end = (
        datetime(year + 1, 1, 1, tzinfo=clock.KYIV)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=clock.KYIV)
    )
    row = (
        await session.execute(
            select(Payment.id).where(
                Payment.provider_id == provider_id,
                Payment.paid_at >= start,
                Payment.paid_at < end,
            )
        )
    ).first()
    return row is not None


async def compute_pending_nudges(
    session: AsyncSession, now: datetime | None = None
) -> list[PendingNudge]:
    """Pure logic: which payment nudges should fire right now."""
    now = now or clock.now()
    cycle = clock.cycle_of(now)
    today = now.day

    providers = (
        (await session.execute(select(Provider).where(Provider.due_day.is_not(None))))
        .scalars()
        .all()
    )

    pending: list[PendingNudge] = []
    for prov in providers:
        due = prov.due_day
        if due is None:  # filtered in SQL, but keep the type checker honest
            continue
        # Inside the nudge window: due_day-3 … due_day.
        if not (due - NUDGE_WINDOW_DAYS <= today <= due):
            continue
        if await _paid_this_cycle(session, prov.id, cycle):
            continue

        nudge = (
            await session.execute(
                select(NudgeLog).where(
                    NudgeLog.provider_id == prov.id,
                    NudgeLog.cycle == cycle,
                    NudgeLog.kind == NudgeKind.payment,
                )
            )
        ).scalar_one_or_none()

        if nudge is not None:
            if (
                nudge.snoozed_until is not None
                and clock.ensure_aware(nudge.snoozed_until) > now
            ):
                continue  # snoozed
            nudged_day = clock.ensure_aware(nudge.nudged_at).astimezone(clock.KYIV).date()
            if nudged_day == now.astimezone(clock.KYIV).date():
                continue  # already nudged today

        pending.append(
            PendingNudge(
                provider_id=prov.id,
                provider_name=prov.name,
                due_day=due,
                expected_amount=(
                    str(prov.expected_amount)
                    if prov.expected_amount is not None
                    else None
                ),
                near_deadline=today >= due - 1,
            )
        )
    return pending


async def run_payment_nudges(
    send: Callable[[int, str], Awaitable[None]], now: datetime | None = None
) -> list[PendingNudge]:
    """Compute + dispatch nudges, recording a NudgeLog for each. Returns those sent."""
    now = now or clock.now()
    cycle = clock.cycle_of(now)
    async with session_scope() as session:
        pending = await compute_pending_nudges(session, now)
        for item in pending:
            existing = (
                await session.execute(
                    select(NudgeLog).where(
                        NudgeLog.provider_id == item.provider_id,
                        NudgeLog.cycle == cycle,
                        NudgeLog.kind == NudgeKind.payment,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    NudgeLog(
                        provider_id=item.provider_id,
                        cycle=cycle,
                        kind=NudgeKind.payment,
                        nudged_at=now,
                    )
                )
            else:
                existing.nudged_at = now

    # Send outside the DB transaction.
    for item in pending:
        await send(get_settings().telegram_allowed_user_id, item.message())
    return pending


# --- meter nudges (L2) -----------------------------------------------------


@dataclass
class PendingMeterNudge:
    provider_id: int
    provider_name: str
    meter_window: int
    category: str

    def message(self) -> str:
        if self.category == Category.gas.value:
            what = "показники газу"
        elif self.category == Category.water.value:
            what = "показники води"
        else:
            what = "показники лічильника"
        return f"До {self.meter_window}-го — {what}. Кинь фото лічильника, зчитаю сам."


async def _reading_done_this_cycle(
    session: AsyncSession, provider_id: int, cycle: str
) -> bool:
    row = (
        await session.execute(
            select(MeterReading.id).where(
                MeterReading.provider_id == provider_id,
                MeterReading.cycle == cycle,
                MeterReading.status.in_((MeterStatus.validated, MeterStatus.submitted)),
            )
        )
    ).first()
    return row is not None


async def compute_pending_meter_nudges(
    session: AsyncSession, now: datetime | None = None
) -> list[PendingMeterNudge]:
    """Pure logic: which meter nudges should fire right now (gas ≤5, water per ВК)."""
    now = now or clock.now()
    cycle = clock.cycle_of(now)
    today = now.day

    providers = (
        (
            await session.execute(
                select(Provider).where(Provider.meter_window.is_not(None))
            )
        )
        .scalars()
        .all()
    )

    pending: list[PendingMeterNudge] = []
    for prov in providers:
        window = prov.meter_window
        if window is None or not meters.window_open(window, today):
            continue
        if await _reading_done_this_cycle(session, prov.id, cycle):
            continue

        nudge = (
            await session.execute(
                select(NudgeLog).where(
                    NudgeLog.provider_id == prov.id,
                    NudgeLog.cycle == cycle,
                    NudgeLog.kind == NudgeKind.meter,
                )
            )
        ).scalar_one_or_none()
        if nudge is not None:
            if (
                nudge.snoozed_until is not None
                and clock.ensure_aware(nudge.snoozed_until) > now
            ):
                continue  # snoozed
            nudged_day = clock.ensure_aware(nudge.nudged_at).astimezone(clock.KYIV).date()
            if nudged_day == now.astimezone(clock.KYIV).date():
                continue  # already nudged today

        pending.append(
            PendingMeterNudge(
                provider_id=prov.id,
                provider_name=prov.name,
                meter_window=window,
                category=prov.category.value,
            )
        )
    return pending


async def run_meter_nudges(
    send: Callable[[int, str], Awaitable[None]], now: datetime | None = None
) -> list[PendingMeterNudge]:
    """Compute + dispatch meter nudges, recording a NudgeLog(kind=meter) for each."""
    now = now or clock.now()
    cycle = clock.cycle_of(now)
    async with session_scope() as session:
        pending = await compute_pending_meter_nudges(session, now)
        for item in pending:
            existing = (
                await session.execute(
                    select(NudgeLog).where(
                        NudgeLog.provider_id == item.provider_id,
                        NudgeLog.cycle == cycle,
                        NudgeLog.kind == NudgeKind.meter,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    NudgeLog(
                        provider_id=item.provider_id,
                        cycle=cycle,
                        kind=NudgeKind.meter,
                        nudged_at=now,
                    )
                )
            else:
                existing.nudged_at = now

    for item in pending:
        await send(get_settings().telegram_allowed_user_id, item.message())
    return pending


# --- scheduler wiring ------------------------------------------------------


def build_scheduler() -> AsyncIOScheduler:
    """AsyncIO scheduler with a Redis jobstore, falling back to memory if unreachable."""
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.tz)
    try:
        from apscheduler.jobstores.redis import RedisJobStore
        from redis import Redis

        client = Redis.from_url(settings.redis_url)
        client.ping()  # probe connectivity so we can fall back cleanly
        store = RedisJobStore(
            jobs_key="dvoretskyi.jobs",
            run_times_key="dvoretskyi.run_times",
        )
        store.redis = client
        scheduler.add_jobstore(store, alias="default")
        log.info("reminders: using Redis jobstore")
    except Exception as exc:  # noqa: BLE001
        log.warning("reminders: Redis unavailable (%s); using in-memory jobstore", exc)
    return scheduler


def schedule_jobs(
    scheduler: AsyncIOScheduler, send: Callable[[int, str], Awaitable[None]]
) -> None:
    """Register the daily payment-nudge job."""
    scheduler.add_job(
        run_payment_nudges,
        trigger="cron",
        hour=10,
        minute=0,
        args=[send],
        id="payment_nudges",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_meter_nudges,
        trigger="cron",
        hour=9,
        minute=0,
        args=[send],
        id="meter_nudges",
        replace_existing=True,
        misfire_grace_time=3600,
    )
