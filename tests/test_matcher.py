from __future__ import annotations

from dvoretskyi.mono import matcher


async def test_match_case_insensitive_substring(session, providers):
    prov = await matcher.match(session, "Оплата NAFTOGAZ Львів, особовий 123")
    assert prov is not None and prov.name == "Газ (постачання)"


async def test_match_returns_none_when_no_pattern(session, providers):
    assert await matcher.match(session, "Кав'ярня на розі") is None


def test_is_utility_candidate_by_mcc():
    assert matcher.is_utility_candidate(4900, "whatever") is True
    assert matcher.is_utility_candidate(4814, "telecom topup") is True


def test_is_utility_candidate_grocery_ignored():
    # MCC 5814 (fast food / grocery-ish) with a neutral description → not a candidate.
    assert matcher.is_utility_candidate(5814, "FOP Coffee Point") is False


def test_is_utility_candidate_by_keyword():
    assert matcher.is_utility_candidate(5999, "оплата за ГАЗ") is True
    assert matcher.is_utility_candidate(None, "ОСББ Дах") is True


def test_stable_token_picks_distinctive_word():
    assert matcher.stable_token("GIGABITPLUS payment 250.00 12.06") == "gigabitplus"


async def test_learn_then_match(session, providers):
    gigabit = providers["Інтернет (Gigabit+)"]
    learned = await matcher.learn_pattern(session, gigabit.id, "GIGABITPLUS internet 250")
    assert learned is not None
    await session.commit()

    prov = await matcher.match(session, "GIGABITPLUS internet 250.00 наступний місяць")
    assert prov is not None and prov.name == "Інтернет (Gigabit+)"


async def test_learn_pattern_idempotent(session, providers):
    gigabit = providers["Інтернет (Gigabit+)"]
    first = await matcher.learn_pattern(session, gigabit.id, "gigabitplus 1")
    assert first is not None
    second = await matcher.learn_pattern(session, gigabit.id, "gigabitplus 2")
    # same stable token "gigabitplus" → no duplicate inserted
    assert second is None


async def test_does_not_learn_bare_category_keyword(session, households, providers):
    """A bare category word («Газ») is a substring of every gas description, so learning
    it would let one gas provider hijack the other. It must NOT be learned."""
    from dvoretskyi.db.models import Category, PayChannel, Provider
    from dvoretskyi.mono.matcher import match

    supply = providers["Газ (постачання)"]
    deliv = Provider(
        name="Газ (доставлення)",
        category=Category.gas,
        pay_channel=PayChannel.mono_communal,
        household_id=households["primary"].id,
    )
    session.add(deliv)
    await session.flush()

    learned = await matcher.learn_pattern(session, supply.id, "Газ")
    assert learned is None  # «газ» is a utility keyword → never learned
    await session.commit()
    # → a «Газ (доставлення)» tx is not hijacked to supply (no «газ» pattern exists).
    assert await match(session, "Газ (доставлення)") is None


async def test_shared_provider_auto_routes_by_account_number(
    session, households, providers
):
    """A shared utility (ЛЕЗ in both homes) auto-routes by the особовий рахунок: one tap
    per address learns its account number, then every later payment carrying it routes to
    the right property — no more prompting."""
    from dvoretskyi.db.models import Category, PayChannel, Provider
    from dvoretskyi.mono.matcher import match

    home_lez = Provider(
        name="Електроенергія (ЛЕЗ)",
        category=Category.electricity,
        pay_channel=PayChannel.mono_communal,
        household_id=households["primary"].id,
    )
    flat_lez = Provider(
        name="Електроенергія (ЛЕЗ)",
        category=Category.electricity,
        pay_channel=PayChannel.mono_communal,
        household_id=households["secondary"].id,
    )
    session.add_all([home_lez, flat_lez])
    await session.flush()

    # Tap once per address → learns each address's account number (the digit run).
    h = await matcher.learn_pattern(
        session, home_lez.id, "Електроенергія рахунок 100200300"
    )
    f = await matcher.learn_pattern(
        session, flat_lez.id, "Електроенергія рахунок 900800700"
    )
    assert h is not None and h.pattern == "100200300"
    assert f is not None and f.pattern == "900800700"
    await session.commit()

    # Thereafter each account number auto-routes to its own property — no prompt.
    m1 = await match(session, "ЛЕЗ оплата 100200300 за червень")
    m2 = await match(session, "ЛЕЗ оплата 900800700 за червень")
    assert m1 is not None and m1.household_id == households["primary"].id
    assert m2 is not None and m2.household_id == households["secondary"].id
    # A bare «Електроенергія» (no account) still can't be routed → prompt.
    assert await match(session, "Електроенергія") is None


async def test_shared_account_collision_is_dropped(session, households, providers):
    """If the same digit run is fed for two different properties (a shared code, not a
    personal account), it's not distinctive → dropped, both keep prompting."""
    from dvoretskyi.db.models import Category, PayChannel, Provider
    from dvoretskyi.mono.matcher import match

    home = Provider(
        name="Електроенергія (ЛЕЗ)",
        category=Category.electricity,
        pay_channel=PayChannel.mono_communal,
        household_id=households["primary"].id,
    )
    flat = Provider(
        name="Електроенергія (ЛЕЗ)",
        category=Category.electricity,
        pay_channel=PayChannel.mono_communal,
        household_id=households["secondary"].id,
    )
    session.add_all([home, flat])
    await session.flush()

    a = await matcher.learn_pattern(session, home.id, "ЛЕЗ ЄДРПОУ 123456789")
    assert a is not None
    await session.commit()
    # Same number routed to the other property → it's shared, not personal → drop both.
    b = await matcher.learn_pattern(session, flat.id, "ЛЕЗ ЄДРПОУ 123456789")
    assert b is None
    await session.commit()
    assert await match(session, "ЛЕЗ ЄДРПОУ 123456789") is None


async def test_shared_name_provider_never_auto_matches_or_learns(
    session, households, providers
):
    """A utility present in BOTH households (here Газ постачання in primary+secondary)
    has identical descriptions per property → no token distinguishes them. So the matcher
    never auto-routes it and learn_pattern refuses to learn — every such tx prompts for
    the household instead of silently going to whichever learned first."""
    from dvoretskyi.db.models import Category, PayChannel, Provider
    from dvoretskyi.mono.matcher import match

    primary_gas = providers["Газ (постачання)"]
    secondary_gas = Provider(
        name="Газ (постачання)",  # same name in the other household → shared/ambiguous
        category=Category.gas,
        pay_channel=PayChannel.mono_communal,
        household_id=households["secondary"].id,
    )
    session.add(secondary_gas)
    await session.flush()

    # Learning is refused for a shared-name provider (it could never auto-match safely).
    learned = await matcher.learn_pattern(session, primary_gas.id, "OBLGAZ pay 10")
    assert learned is None
    await session.commit()
    # And even a pre-existing pattern pointing at a shared-name provider is ignored.
    from dvoretskyi.db.models import PatternSource, ProviderPattern

    session.add(
        ProviderPattern(
            provider_id=primary_gas.id, pattern="oblgaz", source=PatternSource.learned
        )
    )
    await session.commit()
    assert (await match(session, "OBLGAZ pay 10")) is None  # forced to prompt
