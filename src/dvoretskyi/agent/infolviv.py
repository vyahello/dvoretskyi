"""infolviv.com.ua consumer-portal reader (L2) — the authoritative meter readings.

The portal is an Angular SPA over a JSON API. We authenticate (`POST
/api/account/authentication` with `{account, password}` → `accessToken`), then read the
counters with `Authorization: Bearer <token>`. Each counter carries its last filed
factor (reading) and a `factorEditing` submission window (`startDay`/`endDay`).

Credentials come from env and are **never logged**; the response carries the account
holder's name + address — we read only meter fields and log none of it. On any failure
the reader returns the last cached list (or empty), never raising.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx

from dvoretskyi.config import get_settings

log = logging.getLogger(__name__)

# counterType.id → our meter kind. Water meters are id 2, gas id 1 on the portal.
_KIND_BY_TYPE_ID: dict[object, str] = {1: "gas", 2: "water"}


@dataclass
class InfolvivReading:
    kind: str  # "water" | "gas" | "other"
    account_code: str  # invoiceAccount.code — the «рахунок» the user submits against
    counter_number: str  # physical meter serial (not shown to the user)
    provider: str  # service provider name (Львівводоканал / Газорозподільні мережі …)
    service: str  # human service name
    period: str | None  # invoice period as "YYYY-MM"
    value: Decimal | None  # last filed reading (endFactor)
    difference: Decimal | None  # consumption for that period
    window_start_day: int | None  # factorEditing.startDay
    window_end_day: int | None  # factorEditing.endDay
    window_open: bool  # factorEditing.isEnabled
    counter_id: int | None = None  # counter id — needed later to submit a new reading


# Module-level cache: (monotonic_timestamp, readings). Only successful reads are cached.
_cache: tuple[float, list[InfolvivReading]] | None = None


def clear_cache() -> None:
    global _cache
    _cache = None


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_counter(c: dict) -> InfolvivReading:
    ct = c.get("counterType") or {}
    factors = c.get("factors") or []
    f = factors[0] if factors else {}
    fe = c.get("factorEditing") or {}
    period = None
    ip = f.get("invoicePeriod")
    if isinstance(ip, str) and len(ip) >= 7:
        period = ip[:7]  # "2026-05-01T…" → "2026-05"
    ia = c.get("invoiceAccount") or {}
    return InfolvivReading(
        kind=_KIND_BY_TYPE_ID.get(ct.get("id"), "other"),
        account_code=str(ia.get("code") or ""),
        counter_number=str(c.get("counterNumber") or ""),
        provider=str((c.get("serviceProvider") or {}).get("name") or ""),
        service=str((c.get("service") or {}).get("name") or ""),
        period=period,
        value=_to_decimal(f.get("endFactor")),
        difference=_to_decimal(f.get("difference")),
        window_start_day=fe.get("startDay"),
        window_end_day=fe.get("endDay"),
        window_open=bool(fe.get("isEnabled")),
        counter_id=c.get("id"),
    )


async def fetch_infolviv_readings(
    *, client: httpx.AsyncClient | None = None, use_cache: bool = True
) -> list[InfolvivReading]:
    """Authenticate and read the meter readings filed on infolviv.com.ua.

    Returns [] if creds aren't configured. On a transient failure returns the last
    cached list (or []). `client` is injectable for tests (httpx MockTransport).
    """
    global _cache
    st = get_settings()

    if use_cache and _cache is not None:
        ts, cached = _cache
        if time.monotonic() - ts < st.infolv_ttl_seconds:
            return cached

    if not st.infolv_login or not st.infolv_pwd:
        return []

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            base_url=st.infolv_base_url,
            timeout=30.0,
            headers={"Accept": "application/json"},
        )

    try:
        auth = await client.post(
            st.infolv_auth_path,
            json={"account": st.infolv_login, "password": st.infolv_pwd},
        )
        auth.raise_for_status()
        token = auth.json().get("accessToken")
        if not token:
            return _cache[1] if _cache else []
        resp = await client.get(
            st.infolv_counters_path,
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as exc:
        log.warning("infolviv fetch failed: %s", exc)  # no creds/PII in message
        return _cache[1] if _cache else []
    except (ValueError, KeyError, TypeError):
        return _cache[1] if _cache else []
    finally:
        if owns_client:
            await client.aclose()

    if not isinstance(payload, list):
        return _cache[1] if _cache else []
    readings = [_parse_counter(c) for c in payload if isinstance(c, dict)]
    _cache = (time.monotonic(), readings)
    return readings


async def submit_infolviv_reading(
    counter_id: int,
    current_value: Decimal,
    *,
    client: httpx.AsyncClient | None = None,
) -> None:
    """SCAFFOLD (Phase 3) — file a new «Показник поточний» on infolviv. NOT ENABLED.

    Planned flow (intentionally disabled): the user sends a photo → vision parses it and
    picks the meter → on the user's approval we authenticate (same as the reader) and
    `POST {infolv_submit_path}` with `counter_id` + the new reading value. We never
    submit without an explicit approve, so the call is guarded by NotImplementedError
    until the payload is confirmed against the live form and the approve UX is wired.
    """
    raise NotImplementedError(
        "подача показників на infolviv ще не ввімкнена — "
        "спершу фото→парсинг→апрув (Phase 3)"
    )
