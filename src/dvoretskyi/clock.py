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
