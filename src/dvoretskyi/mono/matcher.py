"""Transaction → provider matching, utility-candidate detection, and pattern learning.

Matching is by `description` (case-insensitive substring over ProviderPattern),
never by MCC — communal MCCs collapse across water/gas/light (spec §4.4).
MCC is used only as one signal for the *candidate* heuristic on unmatched txs.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dvoretskyi.config import get_settings
from dvoretskyi.db.models import PatternSource, Provider, ProviderPattern

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

# Payment aggregators: their name is the tx description (not the real payee), so a
# learned pattern would over-match every payment routed through them (spec §4.5). Such
# txs stay uncategorized → the user is prompted each time instead of being mis-matched.
AGGREGATOR_TOKENS: frozenset[str] = frozenset(
    {"portmone", "easypay", "liqpay", "fondy", "ipay", "city24", "plategka"}
)


async def _ambiguous_provider_ids(session: AsyncSession) -> set[int]:
    """Providers whose NAME is shared across households (ЛЕЗ, Газ доставлення). Their
    descriptions are identical between properties, so no token distinguishes them — they
    must never auto-match; the user picks the household on the categorize prompt."""
    provs = (await session.execute(select(Provider))).scalars().all()
    counts: dict[str, int] = {}
    for p in provs:
        counts[p.name] = counts.get(p.name, 0) + 1
    return {p.id for p in provs if counts[p.name] > 1}


async def match(session: AsyncSession, description: str) -> Provider | None:
    """Return the provider whose pattern is a case-insensitive substring of `description`.

    Longer patterns win (more specific), so a learned full-name pattern beats a
    short seed token if both happen to match. Patterns for a **shared-name** provider
    (same utility in both households) are skipped — such a tx can't be auto-routed to one
    property, so it falls through to the household prompt instead.
    """
    desc = (description or "").casefold()
    ambiguous = await _ambiguous_provider_ids(session)
    rows = (
        (await session.execute(select(ProviderPattern).order_by(ProviderPattern.id)))
        .scalars()
        .all()
    )

    best: ProviderPattern | None = None
    for row in rows:
        if row.provider_id in ambiguous:
            continue
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
    """Learn a pattern from a tx description so the next identical payee auto-logs.

    Idempotent: skips if an identical (provider, pattern) already exists. Returns the
    new pattern, or None if nothing usable / already present.
    """
    token = stable_token(raw_description)
    if not token or token in AGGREGATOR_TOKENS or token in UTILITY_KEYWORDS:
        # Too generic to learn → categorize this tx but leave no pattern (next one
        # prompts again):
        #  • aggregator descriptions (Portmone/EasyPay/…) match every payment routed
        #    through that aggregator;
        #  • a bare category keyword («газ», «вода», «світло») is a substring of EVERY
        #    description in that category, so it would hijack sibling providers — e.g.
        #    a learned «газ» for Газ (постачання) wrongly matches «Газ (доставлення)».
        return None

    # A shared-name provider (same utility in both households — ЛЕЗ, Газ доставлення) is
    # never auto-matched (see `match`): its descriptions are identical between properties,
    # so learning a pattern for it is pointless and would only mislead. Leave none and let
    # every such tx prompt for the household. This also subsumes the cross-household token
    # collision — the only multi-household providers in this app ARE shared-name.
    if provider_id in await _ambiguous_provider_ids(session):
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
