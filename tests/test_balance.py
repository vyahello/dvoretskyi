from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from dvoretskyi.agent import balance
from dvoretskyi.agent.balance import Balance, fetch_gigabit_balance
from dvoretskyi.config import get_settings

# Representative cabinet pages (no real cabinet hit).
_LOGIN_HTML = (
    '<form method="POST" action="/login">'
    '<input type="hidden" name="_token" value="tok-abc123">'
    '<input name="id"><input type="password" name="password"></form>'
)


def _dashboard_html(balance_str: str, date: str = "01.06.2026") -> str:
    return (
        f"<div class='b'>Баланс: {balance_str} грн</div>"
        f"<div class='t'>Останнє поповнення: {date}</div>"
    )


def _client(monkeypatch, dashboard_html: str, calls: list[str] | None = None):
    """An httpx client on a MockTransport that serves login form + dashboard.

    Distinct form/dashboard paths so the handler can branch without cookie state.
    """
    st = get_settings()
    monkeypatch.setattr(st, "gigabit_login", "0000TEST")
    monkeypatch.setattr(st, "gigabit_pwd", "secret")  # not the real one
    monkeypatch.setattr(st, "gigabit_login_form_path", "/login-form")
    monkeypatch.setattr(st, "gigabit_login_path", "/login")
    monkeypatch.setattr(st, "gigabit_dashboard_path", "/cabinet")
    balance.clear_cache()

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(f"{request.method} {request.url.path}")
        path = request.url.path
        if path == "/login-form":
            return httpx.Response(200, text=_LOGIN_HTML)
        if path == "/login":
            return httpx.Response(200, text="ok")
        if path == "/cabinet":
            return httpx.Response(200, text=dashboard_html)
        return httpx.Response(404)

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://cabinet.example",
        follow_redirects=True,
    )


async def test_parses_balance_and_topup_date(monkeypatch):
    async with _client(monkeypatch, _dashboard_html("250,00")) as c:
        bal = await fetch_gigabit_balance(client=c, use_cache=False)
    assert bal.ok
    assert bal.balance == Decimal("250.00")
    assert bal.last_topup == "01.06.2026"


async def test_missing_credentials_returns_not_ok(monkeypatch):
    st = get_settings()
    monkeypatch.setattr(st, "gigabit_login", "")
    monkeypatch.setattr(st, "gigabit_account", "")
    monkeypatch.setattr(st, "gigabit_pwd", "")
    balance.clear_cache()
    bal = await fetch_gigabit_balance(use_cache=False)
    assert not bal.ok and bal.balance is None


async def test_unparseable_dashboard_returns_not_ok(monkeypatch):
    async with _client(monkeypatch, "<div>no balance here</div>") as c:
        bal = await fetch_gigabit_balance(client=c, use_cache=False)
    assert not bal.ok and bal.balance is None


async def test_cache_avoids_second_login(monkeypatch):
    calls: list[str] = []
    async with _client(monkeypatch, _dashboard_html("300,00"), calls=calls) as c:
        first = await fetch_gigabit_balance(client=c, use_cache=True)
        second = await fetch_gigabit_balance(client=c, use_cache=True)
    assert first.balance == second.balance == Decimal("300.00")
    # Only the first call performed HTTP; the second was served from cache.
    assert calls.count("GET /cabinet") == 1


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

    _patch_fetch(Balance(Decimal("120.00"), "01.06.2026", ok=True))
    res = await tools.get_provider_balance(session, "Інтернет (Gigabit+)")
    assert res["need_to_pay"] is True
    assert res["pay_link"] and "120" in res["message"]


async def test_balance_sufficient_no_payment(session, providers, _patch_fetch):
    from dvoretskyi.agent import tools

    _patch_fetch(Balance(Decimal("260.00"), "01.06.2026", ok=True))
    res = await tools.get_provider_balance(session, "Інтернет (Gigabit+)")
    assert res["need_to_pay"] is False
    assert "01.06.2026" in res["message"]


async def test_balance_scrape_failure_is_graceful(session, providers, _patch_fetch):
    from dvoretskyi.agent import tools

    _patch_fetch(Balance(None, None, ok=False, note="кабінет недоступний"))
    res = await tools.get_provider_balance(session, "Інтернет (Gigabit+)")
    assert res["ok"] is False and "не зміг" in res["message"].lower()


async def test_balance_unsupported_provider_raises(session, providers):
    from dvoretskyi.agent import tools

    with pytest.raises(NotImplementedError):
        await tools.get_provider_balance(session, "Газ (постачання)")
