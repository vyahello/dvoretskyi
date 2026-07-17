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
  mp:<reading_id>                send that reading's archived photo («📜 Історія» 📸 tap)
  h:<view>[:<slug>]              «📜 Історія» nav: menu | met | pay | pay:<household-slug>
  st:m:<cycle>:<hh>              📊 stats for one month (the ◀/▶ month strip)
  st:t:<mode>:<hh>               📈 dynamics chart: money | prov | vol
  st:p:<hh>                      📆 period chooser
  st:P:<period>:<hh>             📆 apply a period (cur | prev | 6m | YYYY | all)
  st:h:<hh>:<cycle>              🏘 household chooser (keeps the month being viewed)
  st:H:<slug|-|split>:<period>   🏘 apply a household scope ('-' = both combined)
  st:<slug>|st:split             legacy 2-part form — still honoured so buttons on
                                 messages sent before this deploy keep working
`hh` is a household slug or '-' (both). Slugs, never names: an address is PII and
would not fit Telegram's 64-byte callback budget.
"""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from dvoretskyi import clock
from dvoretskyi.config import get_settings
from dvoretskyi.db.models import Provider

# Persistent main menu (reply keyboard). Labels double as the routing key in bot/app.py.
MENU_UNPAID = "💸 Що сплатити"
MENU_STATS = "📊 Статистика"
MENU_BALANCE = "🌐 Баланс інтернету"
MENU_METERS = "🔢 Мої показники"
MENU_HISTORY = "📜 Історія"
MENU_PAYPLAN = "🗓 Як платити"
MENU_HELP = "❓ Довідка"


def main_keyboard() -> ReplyKeyboardMarkup:
    """Always-on tap menu above the text box (no need to type slash-commands). The two
    meter buttons sit together: «Мої показники» = the current state, «Історія» = the
    month-by-month journal with filing AND payment dates. «Як платити» = the monthly
    plan (when/where/through which service), with pay links."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_UNPAID), KeyboardButton(text=MENU_STATS)],
            [KeyboardButton(text=MENU_METERS), KeyboardButton(text=MENU_HISTORY)],
            [KeyboardButton(text=MENU_PAYPLAN), KeyboardButton(text=MENU_BALANCE)],
            [KeyboardButton(text=MENU_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def links_keyboard(links: Sequence[dict]) -> InlineKeyboardMarkup | None:
    """Tappable pay-link buttons for the payment plan — one per distinct service
    (monobank / ДАХ / Portmone). `links` = [{"url":…, "label":…}]; None when empty."""
    rows = [
        [InlineKeyboardButton(text=link["label"], url=link["url"])]
        for link in links
        if link.get("url") and link.get("label")
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def history_menu_keyboard() -> InlineKeyboardMarkup:
    """«📜 Історія» root: choose readings or payments (each opens its own view with a
    «⬅️ Назад» button), so neither dumps everything into one wall of text."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔢 Показники", callback_data="h:met"),
                InlineKeyboardButton(text="💸 Платежі", callback_data="h:pay"),
            ]
        ]
    )


def history_meters_keyboard(
    photo_items: Sequence[tuple[int, str]],
) -> InlineKeyboardMarkup:
    """Readings view: a «📸 Фото» button per saved photo (mp:<id>) + «⬅️ Назад» to root."""
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"mp:{rid}")]
        for rid, label in photo_items
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="h:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def history_households_keyboard(
    households: Sequence[tuple[str, str]],
) -> InlineKeyboardMarkup:
    """Payments are long across two properties → pick one first. `households` =
    [(slug, name)]; each opens that household's payments. + «⬅️ Назад» to root."""
    rows = [
        [InlineKeyboardButton(text=f"🏠 {name}", callback_data=f"h:pay:{slug}")]
        for slug, name in households
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="h:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def history_back_keyboard(target: str = "h:menu") -> InlineKeyboardMarkup:
    """A lone «⬅️ Назад» button pointing at `target` (the root menu or the household
    chooser), so any leaf view can step back."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=target)]]
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


def stats_month_keyboard(
    cycle: str,
    hh: str = "-",
    *,
    prev_cycle: str | None = None,
    next_cycle: str | None = None,
    multi_household: bool = True,
) -> InlineKeyboardMarkup:
    """The month strip under a one-month stats table.

        ◀ травень │ 📅 червень 2026 │ липень ▶
        📈 Динаміка │ 🏘 Житло

    `prev_cycle`/`next_cycle` are None at the edges of what we can show — before the
    first month with data, and after the current month — so the arrow is simply absent
    rather than walking the user into empty months forever.

    The middle button opens the period chooser: a button that merely re-rendered the
    month already on screen would make Telegram reject the edit with «message is not
    modified», i.e. a dead button that also logs an error.
    """
    strip: list[InlineKeyboardButton] = []
    if prev_cycle:
        strip.append(
            InlineKeyboardButton(
                text=f"◀ {clock.format_cycle_short(prev_cycle)}",
                callback_data=f"st:m:{prev_cycle}:{hh}",
            )
        )
    strip.append(
        InlineKeyboardButton(
            text=f"📅 {clock.format_cycle(cycle)}", callback_data=f"st:p:{hh}"
        )
    )
    if next_cycle:
        strip.append(
            InlineKeyboardButton(
                text=f"{clock.format_cycle_short(next_cycle)} ▶",
                callback_data=f"st:m:{next_cycle}:{hh}",
            )
        )
    second = [InlineKeyboardButton(text="📈 Динаміка", callback_data=f"st:t:money:{hh}")]
    if multi_household:
        # Carry the VIEWED cycle, so picking a property while looking at a past month
        # stays in that month instead of teleporting back to today.
        second.append(
            InlineKeyboardButton(text="🏘 Житло", callback_data=f"st:h:{hh}:{cycle}")
        )
    return InlineKeyboardMarkup(inline_keyboard=[strip, second])


def stats_period_keyboard(hh: str = "-") -> InlineKeyboardMarkup:
    """📆 Період — the ranges worth a tap. Seasons («за зиму») stay conversational:
    they're rarely tapped and every extra row costs more than it earns."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📅 Цей місяць", callback_data=f"st:P:cur:{hh}"
                ),
                InlineKeyboardButton(text="📅 Минулий", callback_data=f"st:P:prev:{hh}"),
            ],
            [
                InlineKeyboardButton(
                    text="📈 Останні 6 міс.", callback_data=f"st:P:6m:{hh}"
                ),
                InlineKeyboardButton(text="📆 Цей рік", callback_data=f"st:P:year:{hh}"),
            ],
            [InlineKeyboardButton(text="∞ Весь час", callback_data=f"st:P:all:{hh}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"st:P:cur:{hh}")],
        ]
    )


def stats_trend_keyboard(mode: str, hh: str = "-") -> InlineKeyboardMarkup:
    """📈 Динаміка — the three axes of "is it going up", with the active one marked.

    Money and m³ are separate charts on purpose: different measures, different scales.
    """
    modes = (("money", "💰 Гроші"), ("prov", "🧾 По послугах"), ("vol", "💧 Обсяг м³"))
    row = [
        InlineKeyboardButton(
            text=("• " + label) if key == mode else label,
            callback_data=f"st:t:{key}:{hh}",
        )
        for key, label in modes
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            row,
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"st:P:cur:{hh}")],
        ]
    )


def stats_household_keyboard(
    households: list[tuple[str, str]], cycle: str
) -> InlineKeyboardMarkup:
    """🏘 Житло — drill into one property, see both combined, or split the total across
    them. `households` = [(slug, name)]."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=f"🏠 {name}", callback_data=f"st:H:{slug}:{cycle}")]
        for slug, name in households
    ]
    rows.append(
        [
            InlineKeyboardButton(text="🏘 Разом", callback_data=f"st:H:-:{cycle}"),
            InlineKeyboardButton(text="🏘 Розподіл", callback_data=f"st:H:split:{cycle}"),
        ]
    )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"st:m:{cycle}:-")])
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


def meter_photo_keyboard(
    items: Sequence[tuple[int, str]],
) -> InlineKeyboardMarkup | None:
    """«📸 Фото» buttons under the «📜 Історія» journal — one per reading whose archived
    photo is still on disk. `items` = [(reading_id, label)]; label already names the
    meter + month. None when nothing has a photo (so the journal goes out plain)."""
    if not items:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"mp:{rid}")]
            for rid, label in items
        ]
    )


def pay_keyboard(url: str, label: str | None = None) -> InlineKeyboardMarkup:
    """A single tappable link button — keeps the long pay URL out of the message text.
    Default label shows the top-up amount, e.g. «💳 Поповнити 200 ₴»."""
    if label is None:
        fee = str(get_settings().gigabit_monthly_fee)
        if "." in fee:  # trim only fractional zeros: 200.00→200, 199.50→199.5
            fee = fee.rstrip("0").rstrip(".")
        label = f"🌐 Поповнити {fee} ₴"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, url=url)]]
    )
