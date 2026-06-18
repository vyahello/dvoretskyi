"""Inline keyboards. Callback data is compact: '<kind>:<...>' within Telegram's 64 bytes.

Callback grammar:
  c:<payment_id>:<provider_id>   categorize payment → provider
  c:<payment_id>:n               «Не комуналка» (delete uncategorized payment)
  s:<provider_id>:<days>         snooze payment reminders for N days
  m:<reading_id>:<provider_id>   route an ambiguous meter photo to a provider
  mc:<reading_id>:ok|re          confirm / re-photograph a needs_confirm reading
  ms:<reading_id>                «відправив» → mark the reading submitted
  sm:<provider_id>:<days>        snooze meter reminders for N days
  sf:<reading_id>                approve → submit the reading now (in the 28+ window)
  se:<reading_id>:<attempt>      «подай раніше» before the window; submits on attempt 3
  md:<reading_id>                delete a stored reading (wrong value entered)
  mdc:<scope>|no                 confirm a scoped delete / cancel.
                                 scope='all' | '<pid>' | '<pid|*>:<cycle>' (by month)
"""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from dvoretskyi.config import get_settings
from dvoretskyi.db.models import Provider

# Persistent main menu (reply keyboard). Labels double as the routing key in bot/app.py.
MENU_HELLO = "🎩 Привіт"
MENU_UNPAID = "💸 Що сплатити"
MENU_STATS = "📊 Статистика"
MENU_BALANCE = "🌐 Баланс інтернету"
MENU_METERS = "🔢 Мої показники"
MENU_HELP = "❓ Довідка"


def main_keyboard() -> ReplyKeyboardMarkup:
    """Always-on tap menu above the text box (no need to type slash-commands)."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_HELLO), KeyboardButton(text=MENU_UNPAID)],
            [KeyboardButton(text=MENU_STATS), KeyboardButton(text=MENU_BALANCE)],
            [KeyboardButton(text=MENU_METERS), KeyboardButton(text=MENU_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def categorize_keyboard(
    payment_id: int,
    providers: Sequence[Provider],
    household_names: dict[int, str] | None = None,
) -> InlineKeyboardMarkup:
    """Provider buttons. A name shared across households (ЛЕЗ, Газ доставлення) is
    suffixed « · <житло>» so the user can tell which property the payment is for; the
    callback already carries the household-specific provider id."""
    household_names = household_names or {}
    dup = {p.name for p in providers if [q.name for q in providers].count(p.name) > 1}
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for prov in providers:
        label = prov.name
        if prov.name in dup and prov.household_id in household_names:
            label = f"{prov.name} · {household_names[prov.household_id]}"
        row.append(
            InlineKeyboardButton(text=label, callback_data=f"c:{payment_id}:{prov.id}")
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


def stats_scope_keyboard(
    households: list[tuple[str, str]],
) -> InlineKeyboardMarkup:
    """Under the combined stats: a button per household to drill into it, plus a
    «compare» button that splits the total by property. `households` = [(slug, name)]."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=f"📊 {name}", callback_data=f"st:{slug}")]
        for slug, name in households
    ]
    rows.append(
        [InlineKeyboardButton(text="🏘 Розподіл по житлах", callback_data="st:split")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def correct_household_keyboard(payment_id: int, label: str) -> InlineKeyboardMarkup:
    """Single «↪ Це <інше житло>» tap to move an auto-logged shared payment to the other
    property (the default is home; this re-points the rare secondary one)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"ch:{payment_id}")]
        ]
    )


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


def _delete_button(reading_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(text="🗑 Видалити", callback_data=f"md:{reading_id}")


def meter_approve_keyboard(reading_id: int) -> InlineKeyboardMarkup:
    """In the submission window (28th+): tap to file, or delete if the value is wrong."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📤 Подати на портал", callback_data=f"sf:{reading_id}"
                )
            ],
            [_delete_button(reading_id)],
        ]
    )


def meter_early_keyboard(reading_id: int, attempt: int = 1) -> InlineKeyboardMarkup:
    """Before the window: «подай раніше» (attempt count rides in the callback so we resist
    twice and file on the 3rd tap, no server-side state), or delete a wrong value."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏳ Подати раніше", callback_data=f"se:{reading_id}:{attempt}"
                )
            ],
            [_delete_button(reading_id)],
        ]
    )


def meter_confirm_delete_keyboard(reading_id: int) -> InlineKeyboardMarkup:
    """needs_confirm reading: confirm / re-photo / delete (wrong number)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Підтвердити", callback_data=f"mc:{reading_id}:ok"
                ),
                InlineKeyboardButton(
                    text="Перефотографувати", callback_data=f"mc:{reading_id}:re"
                ),
            ],
            [_delete_button(reading_id)],
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


def meter_delete_confirm_keyboard(scope: str) -> InlineKeyboardMarkup:
    """Ask before a bulk delete: «✅ Так, видалити» (scope) / «↩️ Ні»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Так, видалити", callback_data=f"mdc:{scope}"
                ),
                InlineKeyboardButton(text="↩️ Ні", callback_data="mdc:no"),
            ]
        ]
    )


def pay_keyboard(url: str, label: str | None = None) -> InlineKeyboardMarkup:
    """A single tappable link button — keeps the long pay URL out of the message text.
    Default label shows the top-up amount, e.g. «💳 Поповнити 200 ₴»."""
    if label is None:
        fee = str(get_settings().gigabit_monthly_fee)
        if "." in fee:  # trim only fractional zeros: 200.00→200, 199.50→199.5
            fee = fee.rstrip("0").rstrip(".")
        label = f"💳 Поповнити {fee} ₴"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, url=url)]]
    )
