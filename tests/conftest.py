"""Test fixtures: a shared in-memory SQLite DB, seed data, and a fake LLMProvider.

The single StaticPool connection keeps the in-memory DB alive across sessions, so
code paths that open their own `session_scope()` (webhook, reminders) share the same
data as the test's `session` fixture.
"""

from __future__ import annotations

from decimal import Decimal

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from komunalka.agent.provider import Decision, LLMProvider
from komunalka.db import session as db_session
from komunalka.db.models import (
    Base,
    Category,
    PatternSource,
    PayChannel,
    Provider,
    ProviderPattern,
)


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Redirect the module-global engine/sessionmaker used by session_scope().
    db_session._engine = eng
    db_session._sessionmaker = async_sessionmaker(
        eng, expire_on_commit=False, class_=AsyncSession
    )
    yield eng
    await eng.dispose()
    db_session._engine = None
    db_session._sessionmaker = None


@pytest_asyncio.fixture
async def session(engine) -> AsyncSession:
    async with db_session.get_sessionmaker()() as s:
        yield s


@pytest_asyncio.fixture
async def providers(session) -> dict[str, Provider]:
    """Seed a small provider set with real (test) match patterns."""
    specs = [
        (
            "Газ (постачання)",
            Category.gas,
            PayChannel.mono_communal,
            True,
            15,
            None,
            ["naftogaz"],
        ),
        (
            "Холодна вода",
            Category.water,
            PayChannel.mono_communal,
            True,
            20,
            Decimal("180.00"),
            ["vodokanal"],
        ),
        (
            "Інтернет (Gigabit+)",
            Category.internet,
            PayChannel.mono_card,
            False,
            10,
            Decimal("250.00"),
            [],
        ),
        ("Кварплата (ДАХ)", Category.housing, PayChannel.mono_card, False, 25, None, []),
    ]
    out: dict[str, Provider] = {}
    for name, cat, ch, auto, due, expected, patterns in specs:
        prov = Provider(
            name=name,
            category=cat,
            pay_channel=ch,
            auto_logged=auto,
            due_day=due,
            expected_amount=expected,
            account_number=None,
        )
        session.add(prov)
        await session.flush()
        for pat in patterns:
            session.add(
                ProviderPattern(
                    provider_id=prov.id, pattern=pat, source=PatternSource.seed
                )
            )
        out[name] = prov
    await session.commit()
    return out


class FakeLLMProvider(LLMProvider):
    """Returns canned Decisions in order (last one repeats). Records calls."""

    def __init__(self, decisions: list[Decision]):
        self._decisions = decisions
        self.calls: list[tuple[str, dict]] = []

    async def decide(self, user_text: str, context: dict) -> Decision:
        self.calls.append((user_text, context))
        idx = min(len(self.calls) - 1, len(self._decisions) - 1)
        return self._decisions[idx]
