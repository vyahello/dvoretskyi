"""Household helpers — stable slugs + lookups.

Code refers to the two properties by slug only ("primary"/"secondary"); the display
names are addresses (personal data) and live in the DB, seeded from env on the VPS. A
neutral fallback label is used when no name is configured, so nothing address-shaped is
ever hardcoded here.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dvoretskyi.db.models import Household

PRIMARY = "primary"
SECONDARY = "secondary"

# Neutral, address-free fallbacks when env hasn't named a household yet.
_FALLBACK = {PRIMARY: "Житло 1", SECONDARY: "Житло 2"}


def fallback_label(slug: str) -> str:
    return _FALLBACK.get(slug, slug)


async def by_slug(session: AsyncSession, slug: str) -> Household | None:
    return (
        await session.execute(select(Household).where(Household.slug == slug))
    ).scalar_one_or_none()


async def primary(session: AsyncSession) -> Household | None:
    """The default household — for unscoped asks and the photo meter flow."""
    row = (
        (await session.execute(select(Household).where(Household.is_primary.is_(True))))
        .scalars()
        .first()
    )
    return row or await by_slug(session, PRIMARY)


async def resolve(session: AsyncSession, text: str | None) -> Household | None:
    """Map a user-supplied household hint (slug or a fragment of the address/name) to a
    Household. Empty/None → None (caller decides the default). Case-insensitive, and a
    partial match on the display name counts so «за Зеленою» finds it."""
    if not text:
        return None
    needle = text.strip().casefold()
    if not needle:
        return None
    households = (await session.execute(select(Household))).scalars().all()
    for h in households:  # exact slug first
        if h.slug.casefold() == needle:
            return h
    for h in households:  # then a fragment of the (env) display name
        name = (h.name or "").casefold()
        if name and (needle in name or name in needle):
            return h
    return None
