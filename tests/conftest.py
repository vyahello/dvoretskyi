"""Test fixtures: a shared in-memory SQLite DB, seed data, and a fake LLMProvider.

The single StaticPool connection keeps the in-memory DB alive across sessions, so
code paths that open their own `session_scope()` (webhook, reminders) share the same
data as the test's `session` fixture.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from dvoretskyi.agent import infolviv
from dvoretskyi.agent.provider import Decision, LLMProvider
from dvoretskyi.agent.vision import MeterRead, VisionProvider
from dvoretskyi.config import get_settings
from dvoretskyi.db import session as db_session
from dvoretskyi.db.models import (
    Base,
    Category,
    Household,
    PatternSource,
    PayChannel,
    Provider,
    ProviderPattern,
)


@pytest.fixture(autouse=True)
def _no_live_infolviv(monkeypatch):
    """Keep the suite hermetic: never reach the live infolviv portal just because a
    developer's local `.env` happens to hold real creds (CI has none, so it was already
    isolated there). With creds blank, `fetch_infolviv_readings()` short-circuits to [].
    Tests that need a portal value monkeypatch `reading_for_kind` in their own body (which
    runs after this fixture); `test_infolviv` sets its own creds + a mock client."""
    st = get_settings()
    monkeypatch.setattr(st, "infolv_login", "", raising=False)
    monkeypatch.setattr(st, "infolv_pwd", "", raising=False)
    infolviv.clear_cache()


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
async def households(session) -> dict[str, Household]:
    """Seed two households with fake (non-PII) names — code refers to them by slug."""
    primary = Household(slug="primary", name="Житло 1", is_primary=True)
    secondary = Household(
        slug="secondary", name="Житло 2", is_primary=False, infolviv_account_code="ACC-B"
    )
    session.add_all([primary, secondary])
    await session.commit()
    return {"primary": primary, "secondary": secondary}


@pytest_asyncio.fixture
async def providers(session, households) -> dict[str, Provider]:
    """Seed a small provider set with real (test) match patterns, in the primary
    household (mirrors production where the home household owns the default providers)."""
    # (name, category, pay_channel, auto_logged, due_day, expected, patterns,
    #  meter_window=lead days before month end, meter_decimals)
    specs = [
        (
            "Газ (постачання)",
            Category.gas,
            PayChannel.mono_communal,
            True,
            15,
            None,
            ["naftogaz"],
            3,
            2,
        ),
        (
            "Холодна вода",
            Category.water,
            PayChannel.mono_communal,
            True,
            20,
            Decimal("180.00"),
            ["vodokanal"],
            3,
            3,
        ),
        (
            "Інтернет (Gigabit+)",
            Category.internet,
            PayChannel.mono_card,
            False,
            10,
            Decimal("250.00"),
            [],
            None,
            0,
        ),
        (
            "Кварплата (ДАХ)",
            Category.housing,
            PayChannel.mono_card,
            False,
            25,
            None,
            [],
            None,
            0,
        ),
    ]
    out: dict[str, Provider] = {}
    for name, cat, ch, auto, due, expected, patterns, meter_window, decimals in specs:
        prov = Provider(
            name=name,
            category=cat,
            pay_channel=ch,
            auto_logged=auto,
            due_day=due,
            expected_amount=expected,
            account_number=None,
            meter_window=meter_window,
            meter_decimals=decimals,
            household_id=households["primary"].id,
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


class FakeTranscriptionProvider:
    """Returns a canned transcript — no real Whisper call. `text=""` simulates a
    transcription failure (unintelligible audio)."""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls: list[str] = []

    async def transcribe(self, audio_path: str) -> str:
        self.calls.append(audio_path)
        return self.text


class FakeVisionProvider(VisionProvider):
    """Returns a canned MeterRead — no real `claude` call. `value=None` simulates an
    OCR failure."""

    def __init__(
        self, value, raw: str = "", note: str = "", kind: str = "", comment: str = ""
    ):
        self.value = value
        self.raw = raw or (str(value) if value is not None else "")
        self.note = note
        self.kind = kind
        self.comment = comment
        self.calls: list[str] = []

    async def read_meter(self, image_path: str, hint=None) -> MeterRead:
        self.calls.append(image_path)
        return MeterRead(
            value=self.value,
            raw=self.raw,
            note=self.note,
            kind=self.kind,
            comment=self.comment,
        )
