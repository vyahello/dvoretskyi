from __future__ import annotations

import pickle

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from dvoretskyi.reminders import engine


def _closure_notifier():
    """A sender that closes over local state — mirrors app.py lifespan's `_send`,
    which is NOT picklable. The scheduled jobs must not capture it as a job arg."""
    sent: list[tuple[int, str]] = []

    async def _send(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    return _send


def test_scheduled_jobs_are_picklable_for_redis_jobstore():
    # Bare scheduler (default memory jobstore — no Redis needed for the test).
    scheduler = AsyncIOScheduler()
    engine.schedule_jobs(scheduler, _closure_notifier())

    jobs = scheduler.get_jobs()
    assert {j.id for j in jobs} == {"payment_nudges", "meter_nudges", "balance_nudges"}
    # The Redis jobstore pickles the job's func + args; the old code captured the
    # closure in args (-> "Can't pickle local object"). Both must pickle now.
    for job in jobs:
        pickle.dumps(job.func)
        pickle.dumps(job.args)
