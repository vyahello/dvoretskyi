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


async def test_learn_pattern_cross_household_collision_drops_both(
    session, households, providers
):
    """Same description token for the same utility in two properties → we can't tell them
    apart, so the colliding learned pattern is dropped and nothing auto-matches (the bot
    keeps prompting which household)."""
    from dvoretskyi.db.models import Category, PayChannel, Provider
    from dvoretskyi.mono.matcher import match

    primary_gas = providers["Газ (постачання)"]
    secondary_gas = Provider(
        name="Газ (постачання)",
        category=Category.gas,
        pay_channel=PayChannel.mono_communal,
        household_id=households["secondary"].id,
    )
    session.add(secondary_gas)
    await session.flush()

    # First property learns the token.
    learned = await matcher.learn_pattern(session, primary_gas.id, "OBLGAZ pay 10")
    assert learned is not None
    await session.commit()
    assert (await match(session, "OBLGAZ pay 10")) is not None  # auto-matches for now

    # Same token now routed to the OTHER household → collision → drop, learn nothing.
    clash = await matcher.learn_pattern(session, secondary_gas.id, "OBLGAZ pay 10 again")
    assert clash is None
    await session.commit()
    # Neither household auto-matches anymore — every such tx prompts.
    assert (await match(session, "OBLGAZ pay 10 again")) is None
