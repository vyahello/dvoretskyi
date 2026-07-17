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


_UA_MONTHS_SHORT = (
    "",
    "січ",
    "лют",
    "бер",
    "кві",
    "тра",
    "чер",
    "лип",
    "сер",
    "вер",
    "жов",
    "лис",
    "гру",
)


_UA_MONTHS_GEN = (
    "",
    "січня",
    "лютого",
    "березня",
    "квітня",
    "травня",
    "червня",
    "липня",
    "серпня",
    "вересня",
    "жовтня",
    "листопада",
    "грудня",
)


def format_cycle_genitive(cycle: str) -> str:
    """'2026-05' → 'травня' — the genitive form, so a comparison reads «до травня»
    rather than the ungrammatical «до травень». Malformed → raw key."""
    try:
        _, month = cycle.split("-")
        return _UA_MONTHS_GEN[int(month)]
    except (ValueError, IndexError):
        return cycle


_UA_MONTHS_LOC = (
    "",
    "січні",
    "лютому",
    "березні",
    "квітні",
    "травні",
    "червні",
    "липні",
    "серпні",
    "вересні",
    "жовтні",
    "листопаді",
    "грудні",
)


def format_cycle_locative(cycle: str) -> str:
    """'2026-05' → 'травні' — the locative, for «як у травні». Ukrainian picks the case
    from the preposition: «до» takes the genitive (травня), «у» the locative (травні),
    so the two comparison phrasings need different forms. Malformed → raw key."""
    try:
        _, month = cycle.split("-")
        return _UA_MONTHS_LOC[int(month)]
    except (ValueError, IndexError):
        return cycle


def format_cycle_short(cycle: str, *, with_year: bool = False) -> str:
    """'2026-06' → 'чер' (or 'чер 26' with_year) — a chart axis tick, where the full
    'червень 2026' would collide with its neighbours. Malformed → raw key."""
    try:
        year, month = cycle.split("-")
        label = _UA_MONTHS_SHORT[int(month)]
        return f"{label} {year[2:]}" if with_year else label
    except (ValueError, IndexError):
        return cycle


def shift_cycle(cycle: str, months: int) -> str:
    """'2026-06' + 1 → '2026-07'; '2026-01' - 1 → '2025-12'. The month strip walks
    with this, so it must be exact across year boundaries in both directions."""
    year, month = (int(p) for p in cycle.split("-"))
    total = year * 12 + (month - 1) + months
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def months_between(start: str, end: str) -> int:
    """Whole months from `start` to `end` ('2026-05'→'2026-07' = 2). Negative when
    `end` precedes `start`."""
    ys, ms = (int(p) for p in start.split("-"))
    ye, me = (int(p) for p in end.split("-"))
    return (ye * 12 + me) - (ys * 12 + ms)
