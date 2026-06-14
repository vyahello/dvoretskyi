"""Gigabit+ ISP cabinet balance reader (L2, cabinet.gigabit.te.ua).

The cabinet is a Laravel + Vue SPA (no captcha). The balance is NOT in the server HTML
— it comes from a JSON action the SPA calls after login. So we: GET the login form for
its CSRF `_token` (+ session cookie), POST credentials, GET the dashboard for the
`<meta name="csrf-token">`, then POST that as `X-CSRF-TOKEN` to the user-state endpoint
and read JSON: balance = `user.bill.deposit`, last top-up = `user.LastPayment.date`.

Result is cached (TTL ~1h). Credentials come from env and are **never logged**; the
account JSON carries personal data — we read only the two fields and log none of it.
On any failure the reader returns `ok=False` rather than raising.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx

from dvoretskyi.config import get_settings

log = logging.getLogger(__name__)


@dataclass
class Balance:
    balance: Decimal | None
    last_topup: str | None  # date string from the cabinet, e.g. "2026-06-14"
    ok: bool
    note: str = ""


# Module-level cache: (monotonic_timestamp, Balance). Only successful reads are cached.
_cache: tuple[float, Balance] | None = None


def clear_cache() -> None:
    global _cache
    _cache = None


def gigabit_pay_link() -> str:
    """Portmone top-up deep link with the contract number + monthly fee pre-filled.
    Falls back to the cabinet base URL if the contract (account) is unknown."""
    st = get_settings()
    account = st.gigabit_login or st.gigabit_account
    if not account:
        return st.gigabit_base_url
    return st.gigabit_pay_url_template.format(
        account=account, amount=f"{st.gigabit_monthly_fee:.2f}"
    )


def mobile_pay_link() -> str:
    """Mobile top-up link. There's no balance API for mobile, so this is purely a "pay"
    link — the operator's Portmone page, with the phone (from mobile_account) substituted
    if the template uses `{phone}`."""
    st = get_settings()
    return st.mobile_pay_url_template.format(phone=st.mobile_account)


def _to_decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None


async def fetch_gigabit_balance(
    *, client: httpx.AsyncClient | None = None, use_cache: bool = True
) -> Balance:
    """Log into the Gigabit+ cabinet and read balance + last top-up date from JSON.

    `client` is injectable for tests (e.g. an httpx.AsyncClient on a MockTransport).
    """
    global _cache
    st = get_settings()

    if use_cache and _cache is not None:
        ts, cached = _cache
        if time.monotonic() - ts < st.gigabit_balance_ttl_seconds:
            return cached

    login = st.gigabit_login or st.gigabit_account
    if not login or not st.gigabit_pwd:
        return Balance(None, None, ok=False, note="немає логіна/пароля Gigabit+ у .env")

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            base_url=st.gigabit_base_url, follow_redirects=True, timeout=20.0
        )

    try:
        form = await client.get(st.gigabit_login_form_path)
        m = re.search(st.gigabit_form_csrf_regex, form.text)
        await client.post(
            st.gigabit_login_path,
            data={
                "_token": m.group(1) if m else "",
                "id": login,
                "password": st.gigabit_pwd,
                "remember": "on",
            },
        )
        page = await client.get(st.gigabit_dashboard_path)
        meta = re.search(st.gigabit_meta_csrf_regex, page.text)
        resp = await client.post(
            st.gigabit_user_api_path,
            headers={
                "X-CSRF-TOKEN": meta.group(1) if meta else "",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
            },
        )
        user = resp.json().get("user", {})
    except httpx.HTTPError as exc:
        log.warning("gigabit balance fetch failed: %s", exc)  # no creds/PII in message
        return Balance(None, None, ok=False, note="кабінет недоступний")
    except (ValueError, KeyError, AttributeError):
        return Balance(None, None, ok=False, note="несподіваний формат відповіді")
    finally:
        if owns_client:
            await client.aclose()

    balance = _to_decimal((user.get("bill") or {}).get("deposit"))
    last_raw = (user.get("LastPayment") or {}).get("date")
    last_topup = str(last_raw).split(" ")[0] if last_raw else None
    if balance is None:
        return Balance(None, last_topup, ok=False, note="не знайшов баланс у відповіді")

    result = Balance(balance, last_topup, ok=True)
    _cache = (time.monotonic(), result)
    return result
