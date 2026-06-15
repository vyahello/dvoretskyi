"""Time helpers — everything is tz-aware Europe/Kyiv."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")


def now() -> datetime:
    """Current tz-aware time in Europe/Kyiv."""
    return datetime.now(KYIV)


def ensure_aware(moment: datetime) -> datetime:
    """Attach Kyiv tz to a naive datetime (e.g. one SQLite returned without tzinfo)."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=KYIV)


def cycle_of(moment: datetime) -> str:
    """Billing cycle key 'YYYY-MM' for a given moment (in Kyiv tz)."""
    local = ensure_aware(moment).astimezone(KYIV)
    return f"{local.year:04d}-{local.month:02d}"


def current_cycle() -> str:
    return cycle_of(now())


_UA_MONTHS = (
    "",
    "січень",
    "лютий",
    "березень",
    "квітень",
    "травень",
    "червень",
    "липень",
    "серпень",
    "вересень",
    "жовтень",
    "листопад",
    "грудень",
)


def format_cycle(cycle: str) -> str:
    """'2026-06' → 'червень 2026'; 'all'/'2026' pass through; malformed → raw key."""
    try:
        year, month = cycle.split("-")
        return f"{_UA_MONTHS[int(month)]} {year}"
    except (ValueError, IndexError):
        return cycle
