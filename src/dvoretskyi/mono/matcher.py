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
# Account-number (особовий рахунок) runs: long digit sequences. ≥6 digits skips amounts
# (16.00) and short codes; the особовий рахунок is the distinctive per-address signal that
# lets a shared utility (ЛЕЗ, Газ доставлення) auto-route to the right property.
_ACCOUNT_RE = re.compile(r"\d{6,}")

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
    short seed token if both happen to match. A **shared-name** provider (same utility in
    both households) auto-matches ONLY via an account-number (digit) pattern — its
    особовий рахунок uniquely identifies the property; a generic letter token shared by
    both properties is ignored, so such a tx falls through to the household prompt.
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
        pat = (row.pattern or "").casefold().strip()
        if not pat or pat not in desc:
            continue
        # Shared-name provider: only an account-number (all-digit) pattern is specific
        # enough to route to one property; skip generic letter tokens.
        if row.provider_id in ambiguous and not pat.isdigit():
            continue
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


def account_token(description: str) -> str:
    """The longest digit run (≥6) — the особовий рахунок that identifies the address. ''
    if none. This is what distinguishes the same utility across the two properties."""
    runs = _ACCOUNT_RE.findall(description or "")
    return max(runs, key=len) if runs else ""


async def learn_pattern(
    session: AsyncSession, provider_id: int, raw_description: str
) -> ProviderPattern | None:
    """Learn a pattern from a tx description so the next identical payee auto-logs.

    Idempotent: skips if an identical (provider, pattern) already exists. Returns the
    new pattern, or None if nothing usable / already present.
    """
    if provider_id in await _ambiguous_provider_ids(session):
        # Shared utility (ЛЕЗ, Газ доставлення): the letter token is identical between
        # properties, so the only thing that distinguishes them is the особовий рахунок.
        # Learn that digit run so the next payment carrying it auto-routes to this very
        # property. No account in the description → nothing distinctive → learn nothing
        # (this tx still prompts, no silent mis-routing).
        token = account_token(raw_description)
        if not token:
            return None
        # Guard: if that exact number already routes to a DIFFERENT provider, it's a
        # shared code (e.g. the payee's EDRPOU), not a personal account → drop it and
        # learn nothing, so both properties keep prompting rather than collapsing.
        clash = (
            (
                await session.execute(
                    select(ProviderPattern).where(ProviderPattern.pattern == token)
                )
            )
            .scalars()
            .all()
        )
        bad = [c for c in clash if c.provider_id != provider_id]
        if bad:
            for c in bad:
                if c.source == PatternSource.learned:
                    await session.delete(c)
            await session.flush()
            return None
    else:
        token = stable_token(raw_description)
        if not token or token in AGGREGATOR_TOKENS or token in UTILITY_KEYWORDS:
            # Too generic to learn → categorize this tx but leave no pattern (next one
            # prompts again):
            #  • aggregator descriptions (Portmone/EasyPay/…) match every payment routed
            #    through that aggregator;
            #  • a bare category keyword («газ», «вода») is a substring of EVERY
            #    description in that category, so it would hijack sibling providers — a
            #    learned «газ» for Газ (постачання) wrongly matches «Газ (доставлення)».
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
