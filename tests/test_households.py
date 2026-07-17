from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from dvoretskyi import clock
from dvoretskyi import households as hh
from dvoretskyi.agent import tools
from dvoretskyi.agent.tools import ToolError
from dvoretskyi.db.models import (
    Category,
    PayChannel,
    Payment,
    PaymentSource,
    Provider,
)


def _pay(provider_id, amount, when):
    return Payment(
        provider_id=provider_id,
        amount_uah=Decimal(amount),
        paid_at=when,
        source=PaymentSource.mono_webhook,
        raw_description="",
    )


async def _two_household_payments(session, households, providers):
    """A primary gas payment (300) + a secondary ЛЕЗ payment (100)."""
    gas = providers["Газ (постачання)"]  # primary (from the fixture)
    sec_lez = Provider(
        name="Електроенергія (ЛЕЗ)",
        category=Category.electricity,
        pay_channel=PayChannel.mono_communal,
        household_id=households["secondary"].id,
    )
    session.add(sec_lez)
    await session.flush()
    now = clock.now()
    session.add_all([_pay(gas.id, "300.00", now), _pay(sec_lez.id, "100.00", now)])
    await session.commit()


async def test_stats_combined_sums_both_households(session, households, providers):
    await _two_household_payments(session, households, providers)
    res = await tools.get_stats(session, period="all")
    assert res["total"] == "400.00" and res["household"] is None
    if res["chart_path"]:
        import os

        os.unlink(res["chart_path"])


async def test_stats_breakdown_by_household(session, households, providers):
    await _two_household_payments(session, households, providers)
    res = await tools.get_stats(session, period="all", breakdown="household")
    by_label = {i["label"]: i["total"] for i in res["items"]}
    assert by_label == {"Житло 1": "300.00", "Житло 2": "100.00"}
    if res["chart_path"]:
        import os

        os.unlink(res["chart_path"])


async def test_stats_household_keyboard_has_a_button_per_household(households):
    from dvoretskyi.bot import keyboards

    hh = [(h.slug, h.name) for h in households.values()]
    kb = keyboards.stats_household_keyboard(hh, "2026-06")
    labels = [b.text for row in kb.inline_keyboard for b in row]
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "🏠 Житло 1" in labels and "🏠 Житло 2" in labels
    assert "st:H:primary:2026-06" in data and "st:H:secondary:2026-06" in data
    assert "st:H:split:2026-06" in data  # the compare-households button
    assert "st:H:-:2026-06" in data  # …and «разом» back to the combined view


async def test_stats_filter_one_household(session, households, providers):
    await _two_household_payments(session, households, providers)
    res = await tools.get_stats(session, period="all", household="secondary")
    assert res["total"] == "100.00" and res["household"] == "secondary"
    # Filtered title names the property.
    assert "Житло 2" in res["message"]
    if res["chart_path"]:
        import os

        os.unlink(res["chart_path"])


async def test_stats_filter_by_provider_category(session, households, providers):
    """«скільки за газ» → both gas providers, water/internet excluded."""
    gas_delivery = Provider(
        name="Газ (доставлення)",
        category=Category.gas,
        pay_channel=PayChannel.mono_communal,
        household_id=households["primary"].id,
    )
    session.add(gas_delivery)
    await session.flush()
    now = clock.now()
    session.add_all(
        [
            _pay(providers["Газ (постачання)"].id, "300.00", now),
            _pay(gas_delivery.id, "50.00", now),
            _pay(providers["Холодна вода"].id, "180.00", now),
        ]
    )
    await session.commit()

    res = await tools.get_stats(session, period="all", provider="газ")
    assert res["total"] == "350.00"  # both gas providers; water excluded
    assert {i["label"] for i in res["items"]} == {
        "Газ (постачання)",
        "Газ (доставлення)",
    }
    assert "Газ" in res["message"]  # scope named in the caption
    if res["chart_path"]:
        import os

        os.unlink(res["chart_path"])


async def test_stats_filter_by_provider_and_household(session, households, providers):
    """The reported bug: «сума за газ на Зеленій 151» → that household's gas only,
    not the whole-household total and not the other property's gas."""
    sec_gas = Provider(
        name="Газ (постачання)",  # same name, secondary household
        category=Category.gas,
        pay_channel=PayChannel.mono_communal,
        household_id=households["secondary"].id,
    )
    session.add(sec_gas)
    await session.flush()
    now = clock.now()
    session.add_all(
        [
            _pay(providers["Газ (постачання)"].id, "300.00", now),  # primary gas
            _pay(sec_gas.id, "120.00", now),  # secondary gas
            _pay(providers["Холодна вода"].id, "180.00", now),  # primary water
        ]
    )
    await session.commit()

    res = await tools.get_stats(
        session, period="all", provider="газ", household="secondary"
    )
    assert res["total"] == "120.00"  # only secondary gas
    assert res["household"] == "secondary"
    assert "Газ" in res["message"] and "Житло 2" in res["message"]
    if res["chart_path"]:
        import os

        os.unlink(res["chart_path"])


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


async def test_categorize_keyboard_labels_shared_names_with_household(
    session, households, providers
):
    from dvoretskyi.bot import keyboards

    sec_gas = Provider(
        name="Газ (постачання)",  # duplicated across households
        category=Category.gas,
        pay_channel=PayChannel.mono_communal,
        household_id=households["secondary"].id,
    )
    session.add(sec_gas)
    await session.commit()
    provs = (await session.execute(select(Provider))).scalars().all()
    hh_names = {h.id: h.name for h in households.values()}
    kb = keyboards.categorize_keyboard(1, provs, hh_names)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    # The shared gas name is suffixed with each property; unique names are left plain.
    assert "Газ (постачання) · Житло 1" in labels
    assert "Газ (постачання) · Житло 2" in labels
    assert "Холодна вода" in labels  # unique → no household suffix
