"""Application configuration via pydantic-settings.

All secrets and tunables come from the environment / .env.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- monobank ---
    mono_token: str = ""
    mono_webhook_secret: str = "change-me"

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_allowed_user_id: int = 0

    # --- infra ---
    database_url: str = "sqlite+aiosqlite:///./dvoretskyi.db"
    redis_url: str = "redis://localhost:6379/0"

    # --- LLM ---
    llm_provider: str = "claude_code"
    claude_bin: str = "claude"
    claude_timeout_seconds: int = 60
    # Vision OCR (reading a meter image) is markedly slower than a text turn — the CLI
    # has to open the image and the model reasons over it. Give it a wider budget.
    claude_vision_timeout_seconds: int = 150

    # --- matching ---
    # NoDecode: pydantic-settings would otherwise JSON-decode this complex field from
    # the dotenv string; we want the "4900,4814" CSV form parsed by the validator below.
    utility_mccs: Annotated[set[int], NoDecode] = Field(
        default_factory=lambda: {4900, 4814, 4816}
    )

    # --- meters (L2, Phase 2) ---
    # Per-category submission channel. Default is the safe ManualAssistChannel for all;
    # sms/web_form are opt-in (Phase 2b). NoDecode: parse the "gas:manual,water:manual"
    # CSV form ourselves rather than letting pydantic JSON-decode the dict.
    submission_channels: Annotated[dict[str, str], NoDecode] = Field(
        default_factory=lambda: {"gas": "manual", "water": "manual"}
    )
    sms_gateway_url: str = ""  # empty → SmsChannel emits an sms: deep link, never POSTs
    ocr_max_long_side: int = 1600  # downscale photos to this long side before OCR
    delta_spike_k: int = 3  # flag consumption > k × median(history)
    delta_abs_cap: Decimal = Decimal("1000")  # …but never flag below this absolute jump
    # Meter-reading nudge lead time: how many days before month end to start nudging
    # (readings are due by the last day of the month). Seeds Provider.meter_window.
    meter_window_days: int = 3

    # Personal account identifier — kept out of code/git, seeded into Provider.
    # account_number. Empty = unknown (left null). gigabit = contract no.
    gigabit_account: str = ""

    # --- pay links per provider (iOS apps / Portmone; no personal data) ---
    # Utilities paid in mono «Комунальні» → open the monobank app; ДАХ → the ДАХ app.
    monobank_pay_url: str = "https://apps.apple.com/app/id1287005205"
    dah_pay_url: str = "https://apps.apple.com/app/id1444141366"
    # Mobile is auto-paid (scheduled mono payment); link is just for a manual top-up.
    mobile_pay_url: str = "https://www.portmone.com.ua/popovnyty-rakhunok-mobilnoho"
    mobile_autopay_day: int = 20  # day-of-month the scheduled mono charge runs

    # --- Gigabit+ balance scraper (L2) — cabinet.gigabit.te.ua (Laravel CSRF form) ---
    gigabit_login: str = ""  # login id; falls back to gigabit_account if empty
    gigabit_pwd: str = ""
    gigabit_base_url: str = "https://cabinet.gigabit.te.ua"
    gigabit_login_form_path: str = "/"  # GET → login form (+ CSRF _token)
    gigabit_login_path: str = "/login"  # POST → submit credentials
    gigabit_dashboard_path: str = "/"  # GET → page carrying the <meta csrf-token>
    # The cabinet is a Vue SPA: balance/last-topup come from a JSON action, not HTML.
    gigabit_user_api_path: str = "/total/reload_user"  # POST → account-state JSON
    gigabit_form_csrf_regex: str = r'name="_token"\s+value="([^"]+)"'
    gigabit_meta_csrf_regex: str = r'name="csrf-token"\s+content="([^"]+)"'
    gigabit_monthly_fee: Decimal = Decimal("200")  # monthly subscription, UAH
    gigabit_balance_ttl_seconds: int = 3600  # cache so we don't log in every call
    # Top-up deep link. {account} (contract no., from gigabit_account/env) and {amount}
    # are injected at runtime — no personal id in code. Empty account → cabinet base URL.
    gigabit_pay_url_template: str = (
        "https://www.portmone.com.ua/r3/popovnyty-internet-gigabitplus-lviv"
        "?contract_number_terminal={account}&contract_bill_amount={amount}"
    )

    # --- misc ---
    tz: str = "Europe/Kyiv"
    public_base_url: str = "https://example.com"

    @field_validator("utility_mccs", mode="before")
    @classmethod
    def _parse_mccs(cls, value: object) -> object:
        """Accept "4900,4814" (env string) as well as a real iterable of ints."""
        if isinstance(value, str):
            return {int(part) for part in value.split(",") if part.strip()}
        return value

    @field_validator("submission_channels", mode="before")
    @classmethod
    def _parse_channels(cls, value: object) -> object:
        """Accept "gas:manual,water:manual" (env string) or a real dict."""
        if isinstance(value, str):
            out: dict[str, str] = {}
            for part in value.split(","):
                part = part.strip()
                if not part:
                    continue
                key, _, chan = part.partition(":")
                out[key.strip()] = chan.strip() or "manual"
            return out
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
