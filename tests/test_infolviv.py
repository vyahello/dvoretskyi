from __future__ import annotations

import json
from decimal import Decimal

import httpx

from dvoretskyi.agent import infolviv
from dvoretskyi.agent.infolviv import fetch_infolviv_readings
from dvoretskyi.config import get_settings

# Trimmed to the fields the reader uses, in the shape the portal returns.
_COUNTERS = [
    {
        "id": 111,  # counter id (used later for submission)
        "counterNumber": "10000001",  # fake — never a real account
        "invoiceAccount": {"code": "ACC-WATER-1"},  # fake рахунок
        "service": {"name": "Централізоване водопостачання (ХВ)"},
        "serviceProvider": {"name": "ВОДОКАНАЛ (тест)"},
        "counterType": {"id": 2, "name": "Холодна вода"},
        "factorEditing": {"isEnabled": True, "startDay": 1, "endDay": 10},
        "factors": [
            {
                "invoicePeriod": "2026-05-01T00:00:00Z",
                "endFactor": 100.500,
                "difference": 0.0,
            }
        ],
    },
    {
        "id": 222,
        "counterNumber": "10000002",  # fake — never a real account
        "invoiceAccount": {"code": "ACC-GAS-2"},  # fake рахунок
        "service": {"name": "Розподіл газу"},
        "serviceProvider": {"name": "Газорозподіл (тест)"},
        "counterType": {"id": 1, "name": "Газовий"},
        "factorEditing": {"isEnabled": True, "startDay": 4, "endDay": 10},
        "factors": [
            {
                "invoicePeriod": "2026-05-01T00:00:00Z",
                "endFactor": 2000.25,
                "difference": 3.5,
            }
        ],
    },
]


def _client(monkeypatch, *, auth=200, counters_resp=None, calls=None):
    st = get_settings()
    monkeypatch.setattr(st, "infolv_login", "user@example.com")
    monkeypatch.setattr(st, "infolv_pwd", "secret")  # not real
    monkeypatch.setattr(st, "infolv_auth_path", "/auth")
    monkeypatch.setattr(st, "infolv_counters_path", "/counters")
    infolviv.clear_cache()

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/auth":
            if auth != 200:
                return httpx.Response(auth)
            return httpx.Response(
                200, json={"accessToken": "jwt.t.t", "tokenType": "Bearer"}
            )
        if request.url.path == "/counters":
            return counters_resp or httpx.Response(200, json=_COUNTERS)
        return httpx.Response(404)

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://infolviv.example"
    )


async def test_parses_water_and_gas_readings(monkeypatch):
    calls: list[str] = []
    async with _client(monkeypatch, calls=calls) as c:
        readings = await fetch_infolviv_readings(client=c, use_cache=False)

    assert [r.kind for r in readings] == ["water", "gas"]
    water, gas = readings
    assert water.account_code == "ACC-WATER-1"  # рахунок, not the meter serial
    assert water.counter_id == 111
    assert water.value == Decimal("100.500")
    assert water.period == "2026-05"
    assert (water.window_start_day, water.window_end_day) == (1, 10)
    assert gas.account_code == "ACC-GAS-2"
    assert gas.value == Decimal("2000.25")
    assert gas.difference == Decimal("3.5")
    assert (gas.window_start_day, gas.window_end_day) == (4, 10)
    # Auth precedes the counters read, and the token is attached.
    assert calls == ["POST /auth", "GET /counters"]


async def test_no_credentials_returns_empty(monkeypatch):
    st = get_settings()
    monkeypatch.setattr(st, "infolv_login", "")
    monkeypatch.setattr(st, "infolv_pwd", "")
    infolviv.clear_cache()
    assert await fetch_infolviv_readings(use_cache=False) == []


async def test_auth_failure_returns_empty_not_raises(monkeypatch):
    async with _client(monkeypatch, auth=401) as c:
        assert await fetch_infolviv_readings(client=c, use_cache=False) == []


async def test_unexpected_payload_returns_empty(monkeypatch):
    bad = httpx.Response(200, json={"oops": True})
    async with _client(monkeypatch, counters_resp=bad) as c:
        assert await fetch_infolviv_readings(client=c, use_cache=False) == []


async def test_submission_disabled_by_default_raises(monkeypatch):
    # Live POST stays off until the body is verified → must not contact the portal.
    import pytest

    from dvoretskyi.agent.infolviv import InfolvivSubmitDisabled, submit_infolviv_reading

    st = get_settings()
    monkeypatch.setattr(st, "infolv_submit_enabled", False)
    with pytest.raises(InfolvivSubmitDisabled):
        await submit_infolviv_reading("water", Decimal("123.45"))


async def test_submission_when_enabled_posts_to_factor_endpoint(monkeypatch):
    # When explicitly enabled it resolves the counter by kind and POSTs the reading.
    st = get_settings()
    monkeypatch.setattr(st, "infolv_submit_enabled", True)
    monkeypatch.setattr(st, "infolv_submit_path", "/factor")
    calls: list[str] = []
    posted: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/auth":
            return httpx.Response(200, json={"accessToken": "jwt.t.t"})
        if request.url.path == "/counters":
            return httpx.Response(200, json=_COUNTERS)
        if request.url.path == "/factor":
            posted["body"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    monkeypatch.setattr(st, "infolv_login", "user@example.com")
    monkeypatch.setattr(st, "infolv_pwd", "secret")
    monkeypatch.setattr(st, "infolv_auth_path", "/auth")
    monkeypatch.setattr(st, "infolv_counters_path", "/counters")
    infolviv.clear_cache()

    from dvoretskyi.agent.infolviv import submit_infolviv_reading

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://infolviv.example"
    ) as c:
        counter_id = await submit_infolviv_reading("gas", Decimal("2010.5"), client=c)

    assert counter_id == 222  # the gas counter from _COUNTERS
    assert "POST /factor" in calls
    assert posted["body"] == [{"counterId": 222, "valueZone1": 2010.5}]
