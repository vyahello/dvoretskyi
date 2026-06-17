"""Admin CLI: seed providers, learn patterns, register the mono webhook.

Usage:
  dvoretskyi init-db                       # create tables (dev; prefer alembic)
  dvoretskyi seed-providers                # seed the 7 providers (idempotent)
  dvoretskyi learn-pattern "<provider>" "<substr>"
  dvoretskyi register-mono-webhook [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal

from sqlalchemy import select

from dvoretskyi import households
from dvoretskyi.config import get_settings
from dvoretskyi.db.models import (
    Base,
    Category,
    Household,
    PatternSource,
    PayChannel,
    Provider,
    ProviderPattern,
)
from dvoretskyi.db.session import get_engine, session_scope
from dvoretskyi.mono import client

# The 7 providers (spec §3a, prompt §Seed data). Patterns are TODO placeholders —
# real mono `description` strings are captured live and added via learn-pattern/bot.
# meter_window (gas/water) is a nudge lead time in days before month end, seeded from
# settings (METER_WINDOW_DAYS). Readings are due by the last day of the month.
_settings = get_settings()
SEED_PROVIDERS = [
    dict(
        name="Холодна вода",
        category=Category.water,
        pay_channel=PayChannel.mono_communal,
        auto_logged=True,
        due_day=20,
        expected_amount=None,
        meter_window=_settings.meter_window_days,
        meter_decimals=3,
    ),
    dict(
        name="Електроенергія (ЛЕЗ)",
        category=Category.electricity,
        pay_channel=PayChannel.mono_communal,
        auto_logged=True,
        due_day=20,
        expected_amount=None,
    ),
    dict(
        name="Газ (постачання)",
        category=Category.gas,
        pay_channel=PayChannel.mono_communal,
        auto_logged=True,
        due_day=20,
        expected_amount=None,
        meter_window=_settings.meter_window_days,
        meter_decimals=2,
    ),
    dict(
        name="Газ (доставлення)",
        category=Category.gas,
        pay_channel=PayChannel.mono_communal,
        auto_logged=True,
        due_day=20,
        expected_amount=None,
    ),
    dict(
        name="Інтернет (Gigabit+)",
        category=Category.internet,
        pay_channel=PayChannel.mono_card,
        auto_logged=False,
        due_day=20,
        expected_amount=None,
        account_number=_settings.gigabit_login or None,  # contract no. from env
    ),
    dict(
        name="Кварплата (ДАХ)",
        category=Category.housing,
        pay_channel=PayChannel.mono_card,
        auto_logged=False,
        due_day=20,
        expected_amount=None,
    ),
    dict(
        name="Мобільний",
        category=Category.mobile,
        pay_channel=PayChannel.mono_communal,
        auto_logged=True,
        due_day=None,  # auto-paid (scheduled mono payment) → no payment reminder
        expected_amount=None,
        # No phone stored. mono lists top-ups under «Поповнення мобільного», not
        # «Комуналка», so it still arrives via the unmatched-tx flow (telecom MCC).
    ),
]

# Secondary household (unoccupied): pays only ЛЕЗ + Газ (доставлення). No payment
# reminders yet (`due_day=None` → "оплати поки там не буде"); the gas provider carries a
# STATIC meter value (filed each month) seeded from env. due_day/static set at seed time.
SECONDARY_PROVIDERS = [
    dict(
        name="Електроенергія (ЛЕЗ)",
        category=Category.electricity,
        pay_channel=PayChannel.mono_communal,
        auto_logged=True,
        due_day=None,
        expected_amount=None,
    ),
    dict(
        name="Газ (доставлення)",
        category=Category.gas,
        pay_channel=PayChannel.mono_communal,
        auto_logged=True,
        due_day=None,
        expected_amount=None,
        meter_window=_settings.meter_window_days,
        meter_decimals=2,
        # static_reading is injected from env at seed time (kept out of code).
    ),
]


def _household_specs() -> list[dict]:
    """Both households, named from env (addresses are PII → never hardcoded). Empty env
    → a neutral fallback label so seeding still works in dev/tests."""
    s = _settings
    return [
        dict(
            slug=households.PRIMARY,
            name=s.household_primary_name
            or households.fallback_label(households.PRIMARY),
            is_primary=True,
            infolviv_account_code=s.household_primary_infolviv_account or None,
        ),
        dict(
            slug=households.SECONDARY,
            name=s.household_secondary_name
            or households.fallback_label(households.SECONDARY),
            is_primary=False,
            infolviv_account_code=s.household_secondary_infolviv_account or None,
        ),
    ]


async def _init_db() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Tables created.")


async def _seed_households(session) -> dict[str, Household]:
    """Upsert both households from env (idempotent). Returns {slug: Household}."""
    out: dict[str, Household] = {}
    for spec in _household_specs():
        existing = (
            await session.execute(select(Household).where(Household.slug == spec["slug"]))
        ).scalar_one_or_none()
        if existing is None:
            existing = Household(**spec)
            session.add(existing)
            await session.flush()
            print(f"Created household {spec['slug']!r}.")  # name (address) unlogged
        else:
            # Backfill env-driven fields if they changed.
            if existing.name != spec["name"]:
                existing.name = spec["name"]
                print(f"Updated name for household {spec['slug']!r}.")  # value unlogged
            if existing.is_primary != spec["is_primary"]:
                existing.is_primary = spec["is_primary"]
            if existing.infolviv_account_code != spec["infolviv_account_code"]:
                existing.infolviv_account_code = spec["infolviv_account_code"]
                print(f"Updated infolviv account for household {spec['slug']!r}.")
        out[spec["slug"]] = existing
    return out


def _secondary_static_gas() -> Decimal | None:
    raw = _settings.household_secondary_static_gas.strip()
    if not raw:
        return None
    try:
        return Decimal(raw.replace(",", "."))
    except (ValueError, ArithmeticError):
        print("WARNING: HOUSEHOLD_SECONDARY_STATIC_GAS is not a number — ignoring.")
        return None


async def _seed_providers() -> None:
    created = 0
    plan = [
        (households.PRIMARY, SEED_PROVIDERS),
        (households.SECONDARY, SECONDARY_PROVIDERS),
    ]
    total = sum(len(specs) for _, specs in plan)
    async with session_scope() as session:
        hs = await _seed_households(session)
        for slug, specs in plan:
            hid = hs[slug].id
            for spec in specs:
                spec = dict(spec, household_id=hid)
                if slug == households.SECONDARY and spec["category"] == Category.gas:
                    spec["static_reading"] = _secondary_static_gas()
                exists = (
                    await session.execute(
                        select(Provider).where(
                            Provider.household_id == hid,
                            Provider.name == spec["name"],
                        )
                    )
                ).scalar_one_or_none()
                if exists is not None:
                    # Backfill fields for providers seeded before these were set.
                    due = spec.get("due_day")
                    if (due is None or isinstance(due, int)) and exists.due_day != due:
                        exists.due_day = due
                        print(f"Updated due_day for {exists.name} → {due}.")
                    window = spec.get("meter_window")
                    if isinstance(window, int) and exists.meter_window != window:
                        exists.meter_window = window
                        print(f"Updated meter_window for {exists.name} → {window}.")
                    decimals = spec.get("meter_decimals")
                    if isinstance(decimals, int) and exists.meter_decimals != decimals:
                        exists.meter_decimals = decimals
                        print(f"Updated meter_decimals for {exists.name} → {decimals}.")
                    account = spec.get("account_number")
                    if isinstance(account, str) and exists.account_number != account:
                        exists.account_number = account
                        print(f"Updated account_number for {exists.name}.")  # unlogged
                    static = spec.get("static_reading")
                    if isinstance(static, Decimal) and exists.static_reading != static:
                        exists.static_reading = static
                        print(f"Updated static_reading for {exists.name}.")  # unlogged
                    continue
                provider = Provider(**spec)
                session.add(provider)
                await session.flush()
                session.add(
                    ProviderPattern(
                        provider_id=provider.id,
                        pattern=f"TODO-{provider.id}",  # placeholder; never matches txs
                        source=PatternSource.seed,
                    )
                )
                created += 1
    print(f"Seeded {created} new provider(s); {total - created} already present.")


async def _learn_pattern(
    provider_name: str, pattern: str, household: str | None = None
) -> None:
    token = pattern.strip().casefold()
    async with session_scope() as session:
        matches = (
            (
                await session.execute(
                    select(Provider).where(Provider.name == provider_name)
                )
            )
            .scalars()
            .all()
        )
        if not matches:
            raise SystemExit(f"Unknown provider: {provider_name!r}")
        if len(matches) == 1:
            prov = matches[0]
        else:
            # Same name in both households (ЛЕЗ / Газ доставлення) → disambiguate.
            want = await households.resolve(session, household) if household else None
            if want is not None:
                prov = next((p for p in matches if p.household_id == want.id), matches[0])
            else:
                primary = await households.primary(session)
                prov = next(
                    (p for p in matches if primary and p.household_id == primary.id),
                    matches[0],
                )
                print(
                    f"NOTE: {provider_name!r} exists in >1 household; "
                    "defaulting to primary (pass --household to target the other)."
                )
        existing = (
            await session.execute(
                select(ProviderPattern).where(
                    ProviderPattern.provider_id == prov.id,
                    ProviderPattern.pattern == token,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            print("Pattern already present.")
            return
        session.add(
            ProviderPattern(
                provider_id=prov.id, pattern=token, source=PatternSource.learned
            )
        )
    print(f"Learned pattern {token!r} → {provider_name}.")


async def _register_webhook(dry_run: bool) -> None:
    req = client.build_webhook_request()
    if dry_run:
        print("[dry-run] would send:\n" + req.describe())
        return
    resp = await client.register_webhook()
    print(f"mono responded {resp.status_code}: {resp.text}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="dvoretskyi")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create tables (dev convenience)")
    sub.add_parser(
        "seed-providers", help="seed households + their providers (idempotent)"
    )

    lp = sub.add_parser("learn-pattern", help="add a description substring → provider")
    lp.add_argument("provider")
    lp.add_argument("pattern")
    lp.add_argument(
        "--household", default=None, help="slug/name when the provider is in both"
    )

    rw = sub.add_parser(
        "register-mono-webhook", help="register the webhook URL with mono"
    )
    rw.add_argument(
        "--dry-run", action="store_true", help="print the request, don't send"
    )

    args = parser.parse_args(argv)

    if args.command == "init-db":
        asyncio.run(_init_db())
    elif args.command == "seed-providers":
        asyncio.run(_seed_providers())
    elif args.command == "learn-pattern":
        asyncio.run(_learn_pattern(args.provider, args.pattern, args.household))
    elif args.command == "register-mono-webhook":
        asyncio.run(_register_webhook(args.dry_run))


if __name__ == "__main__":
    main()
