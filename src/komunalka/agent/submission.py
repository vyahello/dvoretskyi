"""Meter submission channels.

Default is `ManualAssistChannel`: the bot never logs in anywhere — it hands back the
validated value plus how/where to submit it (gas → SMS 4647 or gas.ua; water → ВК),
and the user submits. The reading becomes `validated`; a later "відправив" tap flips
it to `submitted`.

`SmsChannel` / `WebFormChannel` are opt-in (Phase 2b), feature-flagged via
`SUBMISSION_CHANNELS`, and dry-run-first. They never run as the default, and we do not
reverse-engineer provider auth — a channel that needs login stays a documented stub.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from komunalka.config import get_settings
from komunalka.db.models import Category, MeterReading, MeterStatus, Provider

log = logging.getLogger(__name__)

GAS_SMS_NUMBER = "4647"


@dataclass
class SubmitResult:
    status: MeterStatus  # validated (manual) or submitted (channel did it)
    message: str  # butler-voice line for the user
    instructions: str | None = None  # how/where to submit (manual)
    deep_link: str | None = None  # sms:/https: link the user can tap
    submitted: bool = False  # True only if the channel actually sent it


class SubmissionChannel(ABC):
    @abstractmethod
    async def submit(self, provider: Provider, reading: MeterReading) -> SubmitResult: ...


def _gas_sms_body(reading: MeterReading) -> str:
    # gas.ua 4647 expects the account number + reading; the user has the real account.
    acct = provider_account(reading)
    if reading.value is None:
        val = ""
    else:
        decimals = (reading.provider.meter_decimals if reading.provider else 0) or 0
        val = f"{reading.value:.{decimals}f}"
    return f"{acct} {val}".strip()


def provider_account(reading: MeterReading) -> str:
    prov = reading.provider
    return (prov.account_number if prov and prov.account_number else "<рахунок>") or (
        "<рахунок>"
    )


class ManualAssistChannel(SubmissionChannel):
    """DEFAULT: hand back the value + how to submit; do not contact the provider."""

    async def submit(self, provider: Provider, reading: MeterReading) -> SubmitResult:
        val = reading.value
        if provider.category is Category.gas:
            body = _gas_sms_body(reading)
            instr = (
                f"Газ: надішли SMS на {GAS_SMS_NUMBER} з текстом «{body}», "
                "або внеси показник на gas.ua."
            )
            link = f"sms:{GAS_SMS_NUMBER}?body={body.replace(' ', '%20')}"
        elif provider.category is Category.water:
            instr = (
                "Вода: внеси показник у кабінеті Львівводоканалу (сайт або Viber-бот ВК)."
            )
            link = None
        else:
            instr = "Передай показник у кабінеті провайдера."
            link = None

        return SubmitResult(
            status=MeterStatus.validated,
            message=f"✅ {provider.name}: {val}. Лишилось передати — підкажу куди.",
            instructions=instr,
            deep_link=link,
            submitted=False,
        )


class SmsChannel(SubmissionChannel):
    """Opt-in (Phase 2b): format the 4647 SMS body. POSTs only if SMS_GATEWAY_URL is
    set; otherwise emits an sms: deep link (never silently sends)."""

    def __init__(self, dry_run: bool = True) -> None:
        self.dry_run = dry_run

    async def submit(self, provider: Provider, reading: MeterReading) -> SubmitResult:
        body = _gas_sms_body(reading)
        gateway = get_settings().sms_gateway_url
        link = f"sms:{GAS_SMS_NUMBER}?body={body.replace(' ', '%20')}"

        if self.dry_run or not gateway:
            return SubmitResult(
                status=MeterStatus.validated,
                message=f"[dry-run] SMS на {GAS_SMS_NUMBER}: «{body}»",
                instructions=f"SMS на {GAS_SMS_NUMBER}: «{body}»",
                deep_link=link,
                submitted=False,
            )

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    gateway, json={"to": GAS_SMS_NUMBER, "body": body}
                )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("sms gateway failed: %s", exc)
            return SubmitResult(
                status=MeterStatus.validated,
                message="Не вдалося надіслати SMS — передай вручну.",
                instructions=f"SMS на {GAS_SMS_NUMBER}: «{body}»",
                deep_link=link,
                submitted=False,
            )
        return SubmitResult(
            status=MeterStatus.submitted,
            message=f"✅ Надіслав показник SMS на {GAS_SMS_NUMBER}.",
            submitted=True,
        )


class WebFormChannel(SubmissionChannel):
    """Opt-in, EXPERIMENTAL (Phase 2b). Fragile; never the default. Provider login/auth
    is intentionally left unimplemented — we do not reverse-engineer it."""

    def __init__(self, dry_run: bool = True) -> None:
        self.dry_run = dry_run

    async def submit(self, provider: Provider, reading: MeterReading) -> SubmitResult:
        if self.dry_run:
            return SubmitResult(
                status=MeterStatus.validated,
                message=f"[dry-run] вебформа {provider.name}: {reading.value}",
                instructions="Вебформа в режимі dry-run — нічого не відправлено.",
                submitted=False,
            )
        raise NotImplementedError(
            "WebFormChannel live submit needs provider auth — left a stub (spec §7)."
        )


_MANUAL = ManualAssistChannel()


def channel_for(provider: Provider) -> SubmissionChannel:
    """Pick the configured channel for a provider's category (default: manual)."""
    name = get_settings().submission_channels.get(provider.category.value, "manual")
    if name == "sms":
        return SmsChannel(dry_run=True)
    if name == "web_form":
        return WebFormChannel(dry_run=True)
    return _MANUAL
