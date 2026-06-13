"""Transaction → provider matching, utility-candidate detection, and pattern learning.

Matching is by `description` (case-insensitive substring over ProviderPattern),
never by MCC — communal MCCs collapse across water/gas/light (spec §4.4).
MCC is used only as one signal for the *candidate* heuristic on unmatched txs.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from komunalka.config import get_settings
from komunalka.db.models import Provider, ProviderPattern, PatternSource

# Keyword signals that an unmatched tx is probably комуналка even when no MCC hint.
UTILITY_KEYWORDS: tuple[str, ...] = (
    "газ",
    "вода",
    "водоканал",
    "енерг",
    "світло",
    "осбб",
    "домоуправ",
    "квартплат",
    "кварплат",
    "комунал",
    "тепло",
    "інтернет",
    "интернет",
    "провайдер",
)

_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)  # runs of letters (Cyrillic/Latin)


async def match(session: AsyncSession, description: str) -> Provider | None:
    """Return the provider whose pattern is a case-insensitive substring of `description`.

    Longer patterns win (more specific), so a learned full-name pattern beats a
    short seed token if both happen to match.
    """
    desc = (description or "").casefold()
    rows = (
        await session.execute(
            select(ProviderPattern).order_by(ProviderPattern.id)
        )
    ).scalars().all()

    best: ProviderPattern | None = None
    for row in rows:
        pat = (row.pattern or "").casefold().strip()
        if pat and pat in desc:
            if best is None or len(pat) > len(best.pattern):
                best = row
    if best is None:
        return None
    return await session.get(Provider, best.provider_id)


def is_utility_candidate(mcc: int | None, description: str) -> bool:
    """True if an unmatched tx is worth prompting about (utility MCC or keyword hit)."""
    settings = get_settings()
    if mcc is not None and mcc in settings.utility_mccs:
        return True
    desc = (description or "").casefold()
    return any(kw in desc for kw in UTILITY_KEYWORDS)


def stable_token(description: str) -> str:
    """Extract a stable, distinctive token from a tx description to learn as a pattern.

    Picks the longest letter-run (typically the payee name), ignoring digits/dates/
    amounts that vary between payments. Falls back to the cleaned full string.
    """
    tokens = [t for t in _TOKEN_RE.findall(description or "") if len(t) >= 4]
    if tokens:
        return max(tokens, key=len).casefold()
    return (description or "").strip().casefold()


async def learn_pattern(
    session: AsyncSession, provider_id: int, raw_description: str
) -> ProviderPattern | None:
    """Insert a learned pattern from a tx description so the next identical payee auto-logs.

    Idempotent: skips if an identical (provider, pattern) already exists. Returns the
    new pattern, or None if nothing usable / already present.
    """
    token = stable_token(raw_description)
    if not token:
        return None

    existing = (
        await session.execute(
            select(ProviderPattern).where(
                ProviderPattern.provider_id == provider_id,
                ProviderPattern.pattern == token,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None

    pattern = ProviderPattern(
        provider_id=provider_id, pattern=token, source=PatternSource.learned
    )
    session.add(pattern)
    await session.flush()
    return pattern
