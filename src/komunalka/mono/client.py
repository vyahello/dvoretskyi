"""Thin monobank personal-API client — webhook registration (used by the CLI)."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from komunalka.config import get_settings

MONO_API_BASE = "https://api.monobank.ua"


@dataclass
class WebhookRequest:
    """The HTTP request that would register the webhook (printable for --dry-run)."""

    method: str
    url: str
    headers: dict[str, str]
    json: dict[str, str]

    def describe(self) -> str:
        # Mask the token when printing.
        safe_headers = dict(self.headers)
        if "X-Token" in safe_headers:
            tok = safe_headers["X-Token"]
            safe_headers["X-Token"] = (tok[:4] + "…") if tok else "(empty)"
        return (
            f"{self.method} {self.url}\n"
            f"headers={safe_headers}\n"
            f"json={self.json}"
        )


def build_webhook_request(public_base_url: str | None = None) -> WebhookRequest:
    settings = get_settings()
    base = (public_base_url or settings.public_base_url).rstrip("/")
    webhook_url = f"{base}/mono/webhook/{settings.mono_webhook_secret}"
    return WebhookRequest(
        method="POST",
        url=f"{MONO_API_BASE}/personal/webhook",
        headers={"X-Token": settings.mono_token},
        json={"webHookUrl": webhook_url},
    )


async def register_webhook(public_base_url: str | None = None) -> httpx.Response:
    req = build_webhook_request(public_base_url)
    async with httpx.AsyncClient(timeout=30) as client:
        return await client.request(
            req.method, req.url, headers=req.headers, json=req.json
        )
