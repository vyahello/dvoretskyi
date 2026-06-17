from __future__ import annotations

import pytest
from sqlalchemy import select

from dvoretskyi import households as hh
from dvoretskyi.agent import tools
from dvoretskyi.agent.tools import ToolError
from dvoretskyi.db.models import Category, PayChannel, Provider


async def test_primary_returns_the_primary_household(session, households):
    prim = await hh.primary(session)
    assert prim is not None and prim.slug == "primary"


async def test_resolve_by_slug_and_name_fragment(session, households):
    assert (await hh.resolve(session, "secondary")).slug == "secondary"
    # A fragment of the (fake) display name resolves too.
    assert (await hh.resolve(session, "житло 2")).slug == "secondary"
    assert await hh.resolve(session, "") is None
    assert await hh.resolve(session, "nonsuch") is None


async def test_same_name_allowed_across_households(session, households, providers):
    # Шашкевича's ЛЕЗ has the same name as the primary one — the composite unique permits
    # it (a global unique on name would have rejected this).
    secondary_lez = Provider(
        name="Газ (постачання)",  # already in primary via the `providers` fixture
        category=Category.gas,
        pay_channel=PayChannel.mono_communal,
        household_id=households["secondary"].id,
    )
    session.add(secondary_lez)
    await session.commit()
    rows = [p for p in (await session.execute(select(Provider))).scalars()]
    gas = [p for p in rows if p.name == "Газ (постачання)"]
    assert {p.household_id for p in gas} == {
        households["primary"].id,
        households["secondary"].id,
    }


async def test_provider_by_name_disambiguates_by_household(
    session, households, providers
):
    secondary_gas = Provider(
        name="Газ (постачання)",  # already in primary via the `providers` fixture
        category=Category.gas,
        pay_channel=PayChannel.mono_communal,
        household_id=households["secondary"].id,
    )
    session.add(secondary_gas)
    await session.commit()

    # No household hint → primary wins.
    prim = await tools._provider_by_name(session, "Газ (постачання)")
    assert prim.household_id == households["primary"].id
    # Explicit slug → that household's row.
    sec = await tools._provider_by_name(
        session, "Газ (постачання)", household="secondary"
    )
    assert sec.household_id == households["secondary"].id


async def test_provider_by_name_unknown_raises(session, households, providers):
    with pytest.raises(ToolError):
        await tools._provider_by_name(session, "Нема такого")
