"""Admin CLI: seed providers, learn patterns, register the mono webhook.

Usage:
  komunalka init-db                       # create tables (dev; prefer alembic)
  komunalka seed-providers                # seed the 6 providers (idempotent)
  komunalka learn-pattern "<provider>" "<substr>"
  komunalka register-mono-webhook [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from komunalka.config import get_settings
from komunalka.db.models import (
    Base,
    Category,
    PatternSource,
    PayChannel,
    Provider,
    ProviderPattern,
)
from komunalka.db.session import get_engine, session_scope
from komunalka.mono import client

# The 6 providers (spec §3a, prompt §Seed data). Patterns are TODO placeholders —
# real mono `description` strings are captured live and added via learn-pattern/bot.
# meter_window (gas/water) is seeded from settings (GAS_METER_DAY / WATER_METER_DAY).
_settings = get_settings()
SEED_PROVIDERS = [
    dict(
        name="Холодна вода",
        category=Category.water,
        pay_channel=PayChannel.mono_communal,
        auto_logged=True,
        due_day=20,
        expected_amount=None,
        meter_window=_settings.water_meter_day,
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
        meter_window=_settings.gas_meter_day,
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
    ),
    dict(
        name="Кварплата (ДАХ)",
        category=Category.housing,
        pay_channel=PayChannel.mono_card,
        auto_logged=False,
        due_day=20,
        expected_amount=None,
    ),
]


async def _init_db() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Tables created.")


async def _seed_providers() -> None:
    created = 0
    async with session_scope() as session:
        for spec in SEED_PROVIDERS:
            exists = (
                await session.execute(
                    select(Provider).where(Provider.name == spec["name"])
                )
            ).scalar_one_or_none()
            if exists is not None:
                # Backfill meter_window for providers seeded before Phase 2.
                want = spec.get("meter_window")
                if isinstance(want, int) and exists.meter_window != want:
                    exists.meter_window = want
                    print(f"Updated meter_window for {exists.name} → {want}.")
                continue
            provider = Provider(account_number=None, **spec)
            session.add(provider)
            await session.flush()
            session.add(
                ProviderPattern(
                    provider_id=provider.id,
                    pattern=f"TODO-{provider.id}",  # placeholder; never matches real txs
                    source=PatternSource.seed,
                )
            )
            created += 1
    print(
        f"Seeded {created} new provider(s); "
        f"{len(SEED_PROVIDERS) - created} already present."
    )


async def _learn_pattern(provider_name: str, pattern: str) -> None:
    token = pattern.strip().casefold()
    async with session_scope() as session:
        prov = (
            await session.execute(select(Provider).where(Provider.name == provider_name))
        ).scalar_one_or_none()
        if prov is None:
            raise SystemExit(f"Unknown provider: {provider_name!r}")
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
    parser = argparse.ArgumentParser(prog="komunalka")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create tables (dev convenience)")
    sub.add_parser("seed-providers", help="seed the 6 providers (idempotent)")

    lp = sub.add_parser("learn-pattern", help="add a description substring → provider")
    lp.add_argument("provider")
    lp.add_argument("pattern")

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
        asyncio.run(_learn_pattern(args.provider, args.pattern))
    elif args.command == "register-mono-webhook":
        asyncio.run(_register_webhook(args.dry_run))


if __name__ == "__main__":
    main()
