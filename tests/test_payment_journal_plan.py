from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from dvoretskyi import clock
from dvoretskyi.agent import tools
from dvoretskyi.db.models import Payment, PaymentSource


def _payment(provider_id, amount, when, **kw):
    return Payment(
        provider_id=provider_id,
        amount_uah=Decimal(amount),
        paid_at=when,
        source=PaymentSource.mono_webhook,
        raw_description=kw.get("desc", ""),
        mono_tx_id=kw.get("tx"),
    )


# --- get_payment_journal (history WITH dates) ------------------------------


async def test_payment_journal_lists_dates_newest_first(session, providers):
    gas = providers["Газ (постачання)"]
    session.add_all(
        [
            _payment(gas.id, "480.00", datetime(2026, 4, 18, tzinfo=clock.KYIV), tx="a"),
            _payment(gas.id, "510.00", datetime(2026, 5, 19, tzinfo=clock.KYIV), tx="b"),
        ]
    )
    await session.commit()

    res = await tools.get_payment_journal(session, provider_name="газ")
    sec = next(s for s in res["sections"] if s["provider"] == "Газ (постачання)")
    # Newest first, each carries its own payment date and amount.
    assert sec["payments"][0]["date"] == "19.05.2026"
    assert sec["payments"][0]["amount"] == "510.00"
    assert sec["payments"][1]["date"] == "18.04.2026"
    # The dated lines reach the user via the message.
    assert "19.05.2026" in res["message"] and "18.04.2026" in res["message"]
    assert "Історія платежів" in res["message"]


async def test_payment_journal_filters_by_provider(session, providers):
    gas = providers["Газ (постачання)"]
    water = providers["Холодна вода"]
    now = clock.now()
    session.add_all(
        [
            _payment(gas.id, "480.00", now, tx="g"),
            _payment(water.id, "180.00", now, tx="w"),
        ]
    )
    await session.commit()

    res = await tools.get_payment_journal(session, provider_name="вода")
    names = {s["provider"] for s in res["sections"]}
    assert names == {"Холодна вода"}


async def test_payment_journal_period_filter(session, providers):
    gas = providers["Газ (постачання)"]
    session.add_all(
        [
            _payment(gas.id, "100.00", datetime(2025, 7, 5, tzinfo=clock.KYIV), tx="o"),
            _payment(gas.id, "200.00", datetime(2026, 5, 5, tzinfo=clock.KYIV), tx="n"),
        ]
    )
    await session.commit()

    res = await tools.get_payment_journal(session, period="2026-05")
    gas_sec = next(s for s in res["sections"] if s["provider"] == "Газ (постачання)")
    assert len(gas_sec["payments"]) == 1
    assert gas_sec["payments"][0]["date"] == "05.05.2026"


async def test_payment_journal_empty_is_friendly(session, providers):
    res = await tools.get_payment_journal(session)
    assert res["sections"] == []
    assert "порожня" in res["message"]


async def test_payment_journal_orders_by_category(session, providers):
    # Pay one of each available category; sections must come out in the reading order
    # gas, water, electricity, housing, internet, mobile (here: gas, water, housing, net).
    now = clock.now()
    for key, tx in [
        ("Холодна вода", "w"),
        ("Кварплата (ДАХ)", "h"),
        ("Газ (постачання)", "g"),
        ("Інтернет (Gigabit+)", "i"),
    ]:
        session.add(_payment(providers[key].id, "10.00", now, tx=tx))
    await session.commit()

    res = await tools.get_payment_journal(session)
    order = [s["provider"] for s in res["sections"]]
    assert order == [
        "Газ (постачання)",
        "Холодна вода",
        "Кварплата (ДАХ)",
        "Інтернет (Gigabit+)",
    ]


async def test_payment_plan_orders_by_category(session, providers):
    res = await tools.get_payment_plan(session)
    order = [r["provider"] for r in res["rows"]]
    # gas (15) before water (20) before housing/internet — by category, not due day.
    assert order.index("Газ (постачання)") < order.index("Холодна вода")
    assert order.index("Холодна вода") < order.index("Кварплата (ДАХ)")
    assert order.index("Кварплата (ДАХ)") < order.index("Інтернет (Gigabit+)")


# --- get_payment_plan (when / how much / through which service) ------------


async def test_payment_plan_lists_due_and_method(session, providers):
    res = await tools.get_payment_plan(session)
    rows = {r["provider"]: r for r in res["rows"]}

    # Гас due_day=15, paid via monobank «Комуналка».
    assert rows["Газ (постачання)"]["due_day"] == 15
    assert "monobank" in rows["Газ (постачання)"]["method"]
    # Кварплата (ДАХ) → the ДАХ app.
    assert "ДАХ" in rows["Кварплата (ДАХ)"]["method"]
    # Інтернет (Gigabit+) → Portmone.
    assert "Portmone" in rows["Інтернет (Gigabit+)"]["method"]
    # The water provider carries a typical amount → surfaced.
    assert rows["Холодна вода"]["expected_amount"] == "180.00"
    # Human-readable plan text is built tool-side.
    assert "Як і коли платимо" in res["message"]


async def test_payment_plan_links_are_deduped(session, providers):
    res = await tools.get_payment_plan(session)
    urls = [link["url"] for link in res["links"]]
    assert len(urls) == len(set(urls))  # no duplicate service buttons
    assert res["links"]  # at least one pay link present


async def test_payment_plan_notes_mobile_autopay(session, providers):
    from dvoretskyi.db.models import Category, PayChannel, Provider

    session.add(
        Provider(
            name="Мобільний",
            category=Category.mobile,
            pay_channel=PayChannel.mono_communal,
            due_day=None,
            household_id=providers["Газ (постачання)"].household_id,
        )
    )
    await session.commit()

    res = await tools.get_payment_plan(session)
    assert any(a["provider"] == "Мобільний" for a in res["autopay"])
    assert "автосписанням monobank" in res["message"]
