"""Inline keyboards. Callback data is compact: '<kind>:<...>' within Telegram's 64 bytes.

Callback grammar:
  c:<payment_id>:<provider_id>   categorize payment → provider
  c:<payment_id>:n               «Не комуналка» (delete uncategorized payment)
  s:<provider_id>:<days>         snooze payment reminders for N days
  m:<reading_id>:<provider_id>   route an ambiguous meter photo to a provider
  mc:<reading_id>:ok|re          confirm / re-photograph a needs_confirm reading
  ms:<reading_id>                «відправив» → mark the reading submitted
  sm:<provider_id>:<days>        snooze meter reminders for N days
"""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from dvoretskyi.db.models import Provider


def categorize_keyboard(
    payment_id: int, providers: Sequence[Provider]
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for prov in providers:
        row.append(
            InlineKeyboardButton(
                text=prov.name, callback_data=f"c:{payment_id}:{prov.id}"
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton(text="Не комуналка", callback_data=f"c:{payment_id}:n")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def snooze_keyboard(provider_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="+1 день", callback_data=f"s:{provider_id}:1"),
                InlineKeyboardButton(text="+3 дні", callback_data=f"s:{provider_id}:3"),
                InlineKeyboardButton(text="+тиждень", callback_data=f"s:{provider_id}:7"),
            ]
        ]
    )


def meter_route_keyboard(
    reading_id: int, providers: Sequence[Provider]
) -> InlineKeyboardMarkup:
    """«Який лічильник?» — one button per meter provider."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=prov.name, callback_data=f"m:{reading_id}:{prov.id}"
                )
            ]
            for prov in providers
        ]
    )


def meter_confirm_keyboard(reading_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Підтвердити", callback_data=f"mc:{reading_id}:ok"
                ),
                InlineKeyboardButton(
                    text="Перефотографувати", callback_data=f"mc:{reading_id}:re"
                ),
            ]
        ]
    )


def meter_submitted_keyboard(reading_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Відправив ✓", callback_data=f"ms:{reading_id}")]
        ]
    )


def meter_snooze_keyboard(provider_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="+1 день", callback_data=f"sm:{provider_id}:1"),
                InlineKeyboardButton(text="+3 дні", callback_data=f"sm:{provider_id}:3"),
            ]
        ]
    )


def pay_keyboard(url: str, label: str = "💳 Поповнити") -> InlineKeyboardMarkup:
    """A single tappable link button — keeps the long pay URL out of the message text."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, url=url)]]
    )
