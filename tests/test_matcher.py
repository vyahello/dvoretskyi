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


def _two_lez(households):
    from dvoretskyi.db.models import Category, PayChannel, Provider

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
    return home, flat


async def test_shared_provider_defaults_to_home_flat_via_account(
    session, households, providers
):
    """Home is the DEFAULT for a shared utility: it learns a letter token so bare
    «Електроенергія» auto-routes home. The flat is reached only by its own account number
    (a digit run), which beats the letter token when present."""
    from dvoretskyi.mono.matcher import match

    home, flat = _two_lez(households)
    session.add_all([home, flat])
    await session.flush()

    h = await matcher.learn_pattern(session, home.id, "Електроенергія за червень")
    f = await matcher.learn_pattern(session, flat.id, "Електроенергія рахунок 900800700")
    assert h is not None and not h.pattern.isdigit()  # home → letter (default)
    assert f is not None and f.pattern == "900800700"  # flat → its account number
    await session.commit()

    # Bare «Електроенергія» (no account) → home, the default.
    home_match = await match(session, "Електроенергія за липень")
    assert home_match is not None and home_match.household_id == households["primary"].id
    # A payment carrying the flat's account → flat (digit beats the letter token).
    flat_match = await match(session, "Електроенергія 900800700 липень")
    assert (
        flat_match is not None and flat_match.household_id == households["secondary"].id
    )


async def test_flat_account_collision_with_shared_code_is_dropped(
    session, households, providers
):
    """If the flat's «account» digit is actually a shared code (already routes elsewhere),
    it isn't distinctive → dropped, so it can't hijack."""
    home, flat = _two_lez(households)
    session.add_all([home, flat])
    await session.flush()
    # Pretend a digit pattern already points at home (e.g. learned from a numeric desc).
    from dvoretskyi.db.models import PatternSource, ProviderPattern

    session.add(
        ProviderPattern(
            provider_id=home.id, pattern="123456789", source=PatternSource.learned
        )
    )
    await session.commit()
    # Flat tries to learn the same number → collision → dropped (learns nothing).
    b = await matcher.learn_pattern(session, flat.id, "ЛЕЗ 123456789")
    assert b is None


async def test_secondary_shared_does_not_learn_letter_token(
    session, households, providers
):
    """The non-default (secondary) property never learns a generic letter token — that
    would just collide with home. Without an account number it learns nothing."""
    home, flat = _two_lez(households)
    session.add_all([home, flat])
    await session.flush()
    # Secondary, bare «Електроенергія» (no digits) → learns nothing.
    learned = await matcher.learn_pattern(session, flat.id, "Електроенергія")
    assert learned is None
