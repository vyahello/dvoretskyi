from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from dvoretskyi.agent import balance
from dvoretskyi.agent.balance import (
    Balance,
    fetch_gigabit_balance,
    gigabit_pay_link,
    mobile_pay_link,
)
from dvoretskyi.config import get_settings

_LOGIN_HTML = (
    '<form method="POST" action="/login"><input name="_token" value="tok-abc"></form>'
)
_DASH_HTML = '<html><head><meta name="csrf-token" content="meta-xyz"></head></html>'


def _user_json(deposit, date="2026-06-14 19:28:36") -> dict:
    return {"user": {"bill": {"deposit": deposit}, "LastPayment": {"date": date}}}


def _client(monkeypatch, api_response: httpx.Response, calls: list[str] | None = None):
    """httpx client on a MockTransport serving the login → dashboard → JSON-API flow.
    Distinct paths so the handler branches cleanly (no cookie state needed)."""
    st = get_settings()
    monkeypatch.setattr(st, "gigabit_login", "0000TEST")
    monkeypatch.setattr(st, "gigabit_pwd", "secret")  # not the real one
    monkeypatch.setattr(st, "gigabit_login_form_path", "/login-form")
    monkeypatch.setattr(st, "gigabit_login_path", "/login")
    monkeypatch.setattr(st, "gigabit_dashboard_path", "/dash")
    monkeypatch.setattr(st, "gigabit_user_api_path", "/total/reload_user")
    balance.clear_cache()

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(f"{request.method} {request.url.path}")
        path = request.url.path
        if path == "/login-form":
            return httpx.Response(200, text=_LOGIN_HTML)
        if path == "/login":
            return httpx.Response(200, text="ok")
        if path == "/dash":
            return httpx.Response(200, text=_DASH_HTML)
        if path == "/total/reload_user":
            return api_response
        return httpx.Response(404)

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://cabinet.example",
        follow_redirects=True,
    )


async def test_parses_balance_and_topup_date(monkeypatch):
    resp = httpx.Response(200, json=_user_json(400))
    async with _client(monkeypatch, resp) as c:
        bal = await fetch_gigabit_balance(client=c, use_cache=False)
    assert bal.ok
    assert bal.balance == Decimal("400.00")
    assert bal.last_topup == "2026-06-14"  # time stripped


def test_pay_link_injects_contract_and_amount(monkeypatch):
    st = get_settings()
    monkeypatch.setattr(st, "gigabit_login", "")
    monkeypatch.setattr(st, "gigabit_account", "0000TEST")
    link = gigabit_pay_link()
    assert "portmone" in link
    assert "contract_number_terminal=0000TEST" in link
    assert "contract_bill_amount=200.00" in link


def test_pay_link_falls_back_without_account(monkeypatch):
    st = get_settings()
    monkeypatch.setattr(st, "gigabit_login", "")
    monkeypatch.setattr(st, "gigabit_account", "")
    assert gigabit_pay_link() == st.gigabit_base_url


def test_mobile_pay_link_uses_template(monkeypatch):
    st = get_settings()
    monkeypatch.setattr(st, "mobile_account", "0000000000")
    # default template has no {phone} → returns the operator page as-is
    assert mobile_pay_link() == "https://www.portmone.com.ua/r3/kyivstar"
    # a prefilling template substitutes the phone
    monkeypatch.setattr(st, "mobile_pay_url_template", "https://pay.example/?n={phone}")
    assert mobile_pay_link() == "https://pay.example/?n=0000000000"


async def test_mobile_get_provider_balance_returns_pay_link(session, providers):
    from dvoretskyi.agent import tools
    from dvoretskyi.db.models import Category, PayChannel, Provider

    session.add(
        Provider(
            name="Мобільний",
            category=Category.mobile,
            pay_channel=PayChannel.mono_communal,
            due_day=20,
        )
    )
    await session.commit()
    res = await tools.get_provider_balance(session, "Мобільний")
    assert res["ok"] and res["pay_link"]
    assert res["pay_label"] == "💳 Поповнити мобільний 600 ₴"  # default amount on button


async def test_missing_credentials_returns_not_ok(monkeypatch):
    st = get_settings()
    monkeypatch.setattr(st, "gigabit_login", "")
    monkeypatch.setattr(st, "gigabit_account", "")
    monkeypatch.setattr(st, "gigabit_pwd", "")
    balance.clear_cache()
    bal = await fetch_gigabit_balance(use_cache=False)
    assert not bal.ok and bal.balance is None


async def test_unexpected_json_returns_not_ok(monkeypatch):
    resp = httpx.Response(200, json={"user": {"nothing": True}})
    async with _client(monkeypatch, resp) as c:
        bal = await fetch_gigabit_balance(client=c, use_cache=False)
    assert not bal.ok and bal.balance is None


async def test_cache_avoids_second_login(monkeypatch):
    calls: list[str] = []
    resp = httpx.Response(200, json=_user_json(300))
    async with _client(monkeypatch, resp, calls=calls) as c:
        first = await fetch_gigabit_balance(client=c, use_cache=True)
        second = await fetch_gigabit_balance(client=c, use_cache=True)
    assert first.balance == second.balance == Decimal("300.00")
    assert calls.count("POST /total/reload_user") == 1  # second served from cache


# --- get_provider_balance decision logic (fetch mocked) --------------------


@pytest.fixture
def _patch_fetch(monkeypatch):
    def _set(bal: Balance):
        async def fake(**kw):
            return bal

        monkeypatch.setattr("dvoretskyi.agent.balance.fetch_gigabit_balance", fake)

    return _set


async def test_balance_below_fee_needs_payment(session, providers, _patch_fetch):
    from dvoretskyi.agent import tools

    _patch_fetch(Balance(Decimal("120.00"), "2026-06-14", ok=True))
    res = await tools.get_provider_balance(session, "Інтернет (Gigabit+)")
    assert res["need_to_pay"] is True
    assert res["pay_link"] and "120" in res["message"]


async def test_balance_sufficient_no_payment(session, providers, _patch_fetch):
    from dvoretskyi.agent import tools

    _patch_fetch(Balance(Decimal("400.00"), "2026-06-14", ok=True))
    res = await tools.get_provider_balance(session, "Інтернет (Gigabit+)")
    assert res["need_to_pay"] is False
    assert "2026-06-14" in res["message"]


async def test_balance_scrape_failure_is_graceful(session, providers, _patch_fetch):
    from dvoretskyi.agent import tools

    _patch_fetch(Balance(None, None, ok=False, note="кабінет недоступний"))
    res = await tools.get_provider_balance(session, "Інтернет (Gigabit+)")
    assert res["ok"] is False and "не зміг" in res["message"].lower()


async def test_balance_unsupported_provider_raises(session, providers):
    from dvoretskyi.agent import tools

    with pytest.raises(NotImplementedError):
        await tools.get_provider_balance(session, "Газ (постачання)")
