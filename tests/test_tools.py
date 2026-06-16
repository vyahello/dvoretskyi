from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal

import pytest

from dvoretskyi import clock
from dvoretskyi.agent import tools
from dvoretskyi.agent.tools import ToolError
from dvoretskyi.db.models import Payment, PaymentSource
from dvoretskyi.mono import matcher


def _payment(provider_id, amount, when, **kw):
    return Payment(
        provider_id=provider_id,
        amount_uah=Decimal(amount),
        paid_at=when,
        source=PaymentSource.mono_webhook,
        raw_description=kw.get("desc", ""),
        mono_tx_id=kw.get("tx"),
    )


async def test_get_unpaid_lists_open_then_clears(session, providers):
    gas = providers["Газ (постачання)"]
    cycle = clock.current_cycle()

    res = await tools.get_unpaid(session, cycle)
    open_names = {i["provider"] for i in res["open"]}
    assert "Газ (постачання)" in open_names
    assert res["all_clear"] is False

    # Pay gas this cycle → it drops off the open list.
    session.add(_payment(gas.id, "480.00", clock.now(), tx="g1"))
    await session.commit()
    res2 = await tools.get_unpaid(session, cycle)
    assert "Газ (постачання)" not in {i["provider"] for i in res2["open"]}


async def test_get_unpaid_respects_cycle_boundary(session, providers):
    gas = providers["Газ (постачання)"]
    # A payment in a different month must NOT satisfy the current cycle.
    last_month = datetime(2000, 1, 10, tzinfo=clock.KYIV)
    session.add(_payment(gas.id, "480.00", last_month, tx="old"))
    await session.commit()
    res = await tools.get_unpaid(session, clock.current_cycle())
    assert "Газ (постачання)" in {i["provider"] for i in res["open"]}


async def test_get_stats_aggregates_by_provider(session, providers):
    gas = providers["Газ (постачання)"]
    water = providers["Холодна вода"]
    now = clock.now()
    session.add_all(
        [
            _payment(gas.id, "480.00", now, tx="s1"),
            _payment(gas.id, "20.00", now, tx="s2"),
            _payment(water.id, "180.00", now, tx="s3"),
        ]
    )
    await session.commit()

    res = await tools.get_stats(session, period="all", breakdown="provider")
    assert res["total"] == "680.00"
    top = res["items"][0]
    # Single gas provider here → its own line (gas is no longer collapsed).
    assert top["label"] == "Газ (постачання)" and top["total"] == "500.00"
    assert res["chart_path"] and os.path.exists(res["chart_path"])
    os.unlink(res["chart_path"])


async def test_get_stats_breakdown_by_month(session, providers):
    gas = providers["Газ (постачання)"]
    session.add_all(
        [
            _payment(gas.id, "100.00", datetime(2026, 1, 5, tzinfo=clock.KYIV), tx="m1"),
            _payment(gas.id, "200.00", datetime(2026, 2, 5, tzinfo=clock.KYIV), tx="m2"),
        ]
    )
    await session.commit()
    res = await tools.get_stats(session, period="2026", breakdown="month")
    labels = {i["label"] for i in res["items"]}
    assert {"2026-01", "2026-02"} <= labels
    assert res["total"] == "300.00"
    if res["chart_path"]:
        os.unlink(res["chart_path"])


async def test_get_stats_splits_gas_into_supply_and_delivery(session, providers):
    from dvoretskyi.db.models import Category, PayChannel, Provider

    deliv = Provider(
        name="Газ (доставлення)",
        category=Category.gas,
        pay_channel=PayChannel.mono_communal,
        due_day=20,
    )
    session.add(deliv)
    await session.flush()
    gas = providers["Газ (постачання)"]
    now = clock.now()
    session.add_all(
        [
            _payment(gas.id, "300.00", now, tx="gp"),
            _payment(deliv.id, "100.00", now, tx="gd"),
        ]
    )
    await session.commit()

    res = await tools.get_stats(session, period="all", breakdown="provider")
    labels = {i["label"] for i in res["items"]}
    # Gas stays split: supply and delivery each get their own line, never a merged «Газ».
    assert "Газ (постачання)" in labels and "Газ (доставлення)" in labels
    assert "Газ" not in labels
    by_label = {i["label"]: i["total"] for i in res["items"]}
    assert by_label["Газ (постачання)"] == "300.00"
    assert by_label["Газ (доставлення)"] == "100.00"
    if res["chart_path"]:
        os.unlink(res["chart_path"])


async def test_get_unpaid_reports_mobile_autopay_pending(session, providers):
    from dvoretskyi.db.models import Category, PayChannel, Provider

    # Mobile: due_day=None → never in `open` (no nag), but surfaced as auto_pending.
    session.add(
        Provider(
            name="Мобільний",
            category=Category.mobile,
            pay_channel=PayChannel.mono_communal,
        )
    )
    await session.commit()
    res = await tools.get_unpaid(session)
    assert any(a["provider"] == "Мобільний" for a in res["auto_pending"])
    assert all(o["provider"] != "Мобільний" for o in res["open"])


async def test_log_payment_manual(session, providers):
    res = await tools.log_payment_manual(session, "Кварплата (ДАХ)", "742.50")
    assert res["ok"] and res["amount_uah"] == "742.50"


async def test_log_payment_manual_unknown_provider(session, providers):
    with pytest.raises(ToolError):
        await tools.log_payment_manual(session, "Нема такого", "100")


async def test_log_payment_manual_bad_amount(session, providers):
    with pytest.raises(ToolError):
        await tools.log_payment_manual(session, "Кварплата (ДАХ)", "-5")


async def test_categorize_payment_learns_pattern(session, providers):
    # Uncategorized webhook payment lands first.
    session.add(
        _payment(None, "250.00", clock.now(), tx="uc1", desc="EASYPAY gigabitplus net")
    )
    await session.commit()

    res = await tools.categorize_payment(session, "uc1", "Інтернет (Gigabit+)")
    assert res["ok"] and res["provider"] == "Інтернет (Gigabit+)"
    assert res["learned_pattern"] == "gigabitplus"  # longest letter-run wins
    await session.commit()

    # Next identical payee now auto-matches.
    prov = await matcher.match(session, "EASYPAY gigabitplus 250 знову")
    assert prov is not None and prov.name == "Інтернет (Gigabit+)"


async def test_snooze_reminder(session, providers):
    res = await tools.snooze_reminder(session, "Холодна вода", "3")
    assert res["ok"] and res["provider"] == "Холодна вода"


async def test_provider_balance_still_stubbed(session, providers):
    # submit_meter_reading is implemented in Phase 2; balance reads have no source yet.
    with pytest.raises(NotImplementedError):
        await tools.get_provider_balance(session, "Газ (постачання)")
