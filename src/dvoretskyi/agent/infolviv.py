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


class InfolvivSubmitDisabled(RuntimeError):
    """Raised when a live submission is attempted while `infolv_submit_enabled` is off.

    The submit body shape (`setMultipleFactors` → POST /counter/factor) is unverified, so
    the live POST stays off until confirmed. Callers catch this and fall back to handing
    the value back for manual filing — never silently dropping the reading.
    """


def clear_cache() -> None:
    global _cache
    _cache = None


async def _authenticate(client: httpx.AsyncClient) -> str | None:
    """POST credentials → return the Bearer accessToken (or None). Never logs creds."""
    st = get_settings()
    auth = await client.post(
        st.infolv_auth_path,
        json={"account": st.infolv_login, "password": st.infolv_pwd},
    )
    auth.raise_for_status()
    token = auth.json().get("accessToken")
    return token if token else None


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
        token = await _authenticate(client)
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


async def counter_id_for_kind(
    kind: str, *, client: httpx.AsyncClient | None = None, use_cache: bool = True
) -> int | None:
    """The infolviv counter id for our meter `kind` ("water"|"gas"), or None."""
    for r in await fetch_infolviv_readings(client=client, use_cache=use_cache):
        if r.kind == kind and r.counter_id is not None:
            return r.counter_id
    return None


async def submit_infolviv_reading(
    kind: str,
    value: Decimal,
    *,
    client: httpx.AsyncClient | None = None,
) -> int:
    """File a new reading on infolviv for our meter `kind`; return the counter id filed.

    Mirrors the SPA's `setMultipleFactors` → `POST /counter/factor`. The exact JSON body
    is in a lazy-loaded chunk and is **unverified**, so this only runs when
    `infolv_submit_enabled` is True — otherwise it raises `InfolvivSubmitDisabled` and the
    caller falls back to manual filing. Resolves the counter id by `kind`, authenticates
    (same as the reader), then POSTs. Creds/PII are never logged.
    """
    st = get_settings()
    if not st.infolv_submit_enabled:
        raise InfolvivSubmitDisabled(
            "подача на infolviv вимкнена (infolv_submit_enabled=false) — "
            "треба підтвердити тіло запиту POST /counter/factor"
        )
    if not st.infolv_login or not st.infolv_pwd:
        raise InfolvivSubmitDisabled("немає кредів infolviv")

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            base_url=st.infolv_base_url,
            timeout=30.0,
            headers={"Accept": "application/json"},
        )
    try:
        counter_id = await counter_id_for_kind(kind, client=client, use_cache=False)
        if counter_id is None:
            raise InfolvivSubmitDisabled(f"не знайшов лічильник «{kind}» на порталі")
        token = await _authenticate(client)
        if not token:
            raise InfolvivSubmitDisabled("не вдалося авторизуватися на infolviv")
        # setMultipleFactors: a list of factor entries. Single-zone meters → valueZone1.
        # NOTE: field names UNVERIFIED until a real request is captured; gated above.
        body = [{"counterId": counter_id, "valueZone1": float(value)}]
        resp = await client.post(
            st.infolv_submit_path,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
    finally:
        if owns_client:
            await client.aclose()
    clear_cache()  # the filed reading changes last-factors → drop the stale cache
    return counter_id
