from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from komunalka.config import get_settings
from komunalka.db.models import Payment
from komunalka.mono.schemas import StatementItem
from komunalka.mono.webhook import Action, process_statement_item, router


def _item(**kw) -> StatementItem:
    base = dict(id="tx-1", time=1_750_000_000, description="", mcc=4900, amount=-48000)
    base.update(kw)
    return StatementItem(**base)


async def _count_payments(session) -> int:
    return (await session.execute(select(func.count()).select_from(Payment))).scalar_one()


async def test_known_pattern_auto_logs(session, providers):
    res = await process_statement_item(
        session, _item(description="NAFTOGAZ оплата", amount=-48000)
    )
    assert res.action is Action.LOGGED
    assert res.provider.name == "Газ (постачання)"
    assert res.payment.amount_uah == __import__("decimal").Decimal("480.00")
    assert res.payment.mono_tx_id == "tx-1"


async def test_idempotent_duplicate_ignored(session, providers):
    first = await process_statement_item(session, _item(description="NAFTOGAZ"))
    assert first.action is Action.LOGGED
    await session.commit()
    again = await process_statement_item(session, _item(description="NAFTOGAZ"))
    assert again.action is Action.DUPLICATE
    assert await _count_payments(session) == 1


async def test_inflow_ignored(session, providers):
    res = await process_statement_item(
        session, _item(description="NAFTOGAZ", amount=48000)
    )
    assert res.action is Action.INFLOW
    assert await _count_payments(session) == 0


async def test_utility_candidate_unmatched_uncategorized(session, providers):
    # Unmatched description but utility MCC → stored uncategorized for a prompt.
    res = await process_statement_item(
        session,
        _item(id="tx-9", description="EASYPAY *Columbus", mcc=4814, amount=-25000),
    )
    assert res.action is Action.UNCATEGORIZED
    assert res.payment.provider_id is None
    assert res.payment.amount_uah == __import__("decimal").Decimal("250.00")


async def test_non_candidate_ignored(session, providers):
    res = await process_statement_item(
        session, _item(id="tx-7", description="SILPO grocery", mcc=5814, amount=-32000)
    )
    assert res.action is Action.NOT_CANDIDATE
    assert await _count_payments(session) == 0


# --- HTTP route: secret + GET validation ----------------------------------


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.notifier = None
    return TestClient(app)


def test_get_validation_secret(engine):
    secret = get_settings().mono_webhook_secret
    client = _client()
    assert client.get(f"/mono/webhook/{secret}").status_code == 200
    assert client.get("/mono/webhook/wrong").status_code == 403


def test_post_rejects_bad_secret(engine):
    client = _client()
    resp = client.post("/mono/webhook/wrong", json={"type": "StatementItem", "data": {}})
    assert resp.status_code == 403
