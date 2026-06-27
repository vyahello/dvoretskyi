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
from typing import TYPE_CHECKING

import httpx

from dvoretskyi.config import get_settings

if TYPE_CHECKING:
    from dvoretskyi.db.models import Provider

log = logging.getLogger(__name__)


@dataclass
class Balance:
    balance: Decimal | None
    last_topup: str | None  # date string from the cabinet, e.g. "2026-06-14"
    ok: bool
    note: str = ""
    monthly_fee: Decimal | None = None  # «Місячна абонплата» from the cabinet tariff


# Module-level cache: (monotonic_timestamp, Balance). Only successful reads are cached.
_cache: tuple[float, Balance] | None = None


def clear_cache() -> None:
    global _cache
    _cache = None


def gigabit_pay_link(amount: Decimal | None = None) -> str:
    """Portmone top-up deep link with the contract number + monthly fee pre-filled.
    `amount` (e.g. the fee scraped from the cabinet) overrides the config default.
    Falls back to the cabinet base URL if the contract (account) is unknown."""
    st = get_settings()
    account = st.gigabit_login
    if not account:
        return st.gigabit_base_url
    fee = amount if amount is not None else st.gigabit_monthly_fee
    return st.gigabit_pay_url_template.format(account=account, amount=f"{fee:.2f}")


def mobile_pay_link() -> str:
    """Static mobile top-up link (Portmone). No phone number is used or stored."""
    return get_settings().mobile_pay_url


def pay_link_for(provider: Provider) -> tuple[str | None, str | None]:
    """(url, button label) for a provider's payment, or (None, None) if no link.

    Utilities paid in mono «Комунальні» → monobank app; Кварплата (ДАХ) → ДАХ app;
    Gigabit+ → its Portmone top-up (prefilled). iOS App Store / universal links.
    """
    from dvoretskyi.db.models import Category

    st = get_settings()
    name = provider.name.casefold()
    if provider.category is Category.housing:
        return st.dah_pay_url, "📲 Відкрити ДАХ"
    if "gigabit" in name:
        return gigabit_pay_link(), "🌐 Поповнити"
    if provider.category in (Category.water, Category.electricity, Category.gas):
        return st.monobank_pay_url, "📲 Відкрити monobank"
    return None, None


def pay_method_label(provider: Provider) -> str:
    """Human «через що / де» a provider is paid — for the payment plan. Mirrors the
    routing of `pay_link_for` (the confirmed real methods), in spoken Ukrainian.
    Утиліти → monobank «Комуналка»; Кварплата → застосунок ДАХ; Gigabit+ → Portmone;
    Мобільний → автосписання monobank."""
    from dvoretskyi.db.models import Category

    name = provider.name.casefold()
    if provider.category is Category.housing:
        return "застосунок ДАХ"
    if "gigabit" in name:
        return "Portmone (поповнення кабінету Gigabit+)"
    if provider.category is Category.mobile:
        return "автосписання monobank"
    if provider.category in (Category.water, Category.electricity, Category.gas):
        return "monobank, розділ «Комуналка»"
    return "вручну"


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

    login = st.gigabit_login
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
    # Monthly fee straight from the tariff plan, so we don't hardcode 200 ₴.
    tarif = (user.get("dv_main") or {}).get("tarif_plan") or {}
    monthly_fee = _to_decimal(tarif.get("month_fee"))
    if balance is None:
        return Balance(
            None,
            last_topup,
            ok=False,
            note="не знайшов баланс у відповіді",
            monthly_fee=monthly_fee,
        )

    result = Balance(balance, last_topup, ok=True, monthly_fee=monthly_fee)
    _cache = (time.monotonic(), result)
    return result
