from __future__ import annotations

from types import SimpleNamespace

import pytest

from dvoretskyi.bot.app import AllowlistMiddleware
from dvoretskyi.config import Settings, get_settings

# --- Settings.allowed_user_ids ---------------------------------------------


def test_allowed_user_ids_is_owner_plus_extras():
    s = Settings(
        telegram_allowed_user_id=111,
        telegram_extra_allowed_user_ids={222, 333},
    )
    assert s.allowed_user_ids == {111, 222, 333}


def test_allowed_user_ids_owner_only_when_no_extras():
    s = Settings(telegram_allowed_user_id=111)
    assert s.allowed_user_ids == {111}


def test_unset_owner_is_dropped():
    # An unset owner (0) is not a real Telegram id → never allowlisted.
    s = Settings(telegram_allowed_user_id=0, telegram_extra_allowed_user_ids={222})
    assert s.allowed_user_ids == {222}


def test_extra_ids_parse_from_csv_env(monkeypatch):
    # The env arrives as a "111,222" string; the validator parses it to a set of ints.
    get_settings.cache_clear()
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "111")
    monkeypatch.setenv("TELEGRAM_EXTRA_ALLOWED_USER_IDS", "222, 333")
    s = get_settings()
    assert s.telegram_extra_allowed_user_ids == {222, 333}
    assert s.allowed_user_ids == {111, 222, 333}
    get_settings.cache_clear()


def test_extra_ids_default_empty():
    assert Settings(telegram_allowed_user_id=111).telegram_extra_allowed_user_ids == set()


# --- AllowlistMiddleware ----------------------------------------------------


async def _run(allowed: set[int], user_id: int | None):
    calls: list = []

    async def handler(event, data):
        calls.append((event, data))
        return "handled"

    mw = AllowlistMiddleware(allowed)
    data = {
        "event_from_user": SimpleNamespace(id=user_id) if user_id is not None else None
    }
    result = await mw(handler, object(), data)
    return result, calls


@pytest.mark.parametrize("uid", [111, 222, 333])
async def test_middleware_passes_any_allowed_user(uid):
    result, calls = await _run({111, 222, 333}, uid)
    assert result == "handled"
    assert len(calls) == 1


async def test_middleware_drops_stranger():
    result, calls = await _run({111, 222}, 999)
    assert result is None
    assert calls == []  # handler never ran, no reply


async def test_middleware_drops_when_no_user():
    result, calls = await _run({111}, None)
    assert result is None
    assert calls == []
