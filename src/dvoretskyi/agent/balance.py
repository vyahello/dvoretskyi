"""Gigabit+ ISP cabinet balance scraper (L2, cabinet.gigabit.te.ua).

The cabinet is a Laravel app with a plain CSRF login form (no captcha): GET the form
to pick up the `_token` + session cookie, POST credentials, then GET the dashboard and
parse the balance + last top-up date. Selectors/URLs live in config so they can be tuned
against the live page without code changes.

Result is cached (TTL ~1h) so we don't log in on every query. Credentials are read from
env (`gigabit_login`/`gigabit_pwd`, login falling back to `gigabit_account`) and are
**never logged**. On any failure the scraper returns `ok=False` rather than raising.
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
    last_topup: str | None  # raw date string as shown in the cabinet
    ok: bool
    note: str = ""


# Module-level cache: (monotonic_timestamp, Balance). Only successful reads are cached.
_cache: tuple[float, Balance] | None = None


def clear_cache() -> None:
    global _cache
    _cache = None


def _parse_balance(text: str, pattern: str) -> Decimal | None:
    m = re.search(pattern, text)
    if not m:
        return None
    raw = re.sub(r"[\s\u00a0]", "", m.group(1)).replace(",", ".")
    try:
        return Decimal(raw).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _parse_date(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1) if m else None


async def fetch_gigabit_balance(
    *, client: httpx.AsyncClient | None = None, use_cache: bool = True
) -> Balance:
    """Log into the Gigabit+ cabinet and read the balance + last top-up date.

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
        token_match = re.search(st.gigabit_csrf_regex, form.text)
        token = token_match.group(1) if token_match else ""
        await client.post(
            st.gigabit_login_path,
            data={
                "_token": token,
                "id": login,
                "password": st.gigabit_pwd,
                "remember": "on",
            },
        )
        page = await client.get(st.gigabit_dashboard_path)
        balance = _parse_balance(page.text, st.gigabit_balance_regex)
        last_topup = _parse_date(page.text, st.gigabit_topup_date_regex)
    except httpx.HTTPError as exc:
        log.warning("gigabit balance fetch failed: %s", exc)  # no creds in the message
        return Balance(None, None, ok=False, note="кабінет недоступний")
    finally:
        if owns_client:
            await client.aclose()

    if balance is None:
        return Balance(None, last_topup, ok=False, note="не вдалося розпарсити баланс")

    result = Balance(balance, last_topup, ok=True)
    _cache = (time.monotonic(), result)
    return result
