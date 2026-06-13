# CLAUDE.md — Komunalka / Платон (operational notes)

Single-user Telegram utility butler ("Платон"). **Phase 1 scope only:** money + stats
+ reminders + conversational agent, sourced from the monobank webhook. No provider
scraping, no meter OCR/submission, no payment initiation. See `docs/` for the spec
and the Phase 1 build prompt.

## ⚠️ Hard rule: never set ANTHROPIC_API_KEY
Claude Code prioritizes `ANTHROPIC_API_KEY` over the Max subscription and silently
bills the API (documented ~$1,800-in-2-days footgun). The bot **strips it** from the
`claude` subprocess env (`agent/provider.py::ClaudeCodeProvider._child_env`). Do not
add it to `.env`, systemd unit, or shell profile of the service user. Auth Claude Code
via `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`.

## Layout
- `config.py` — pydantic-settings (`get_settings()`), `.env`-driven.
- `clock.py` — tz-aware Europe/Kyiv helpers; cycle = "YYYY-MM".
- `db/` — SQLAlchemy 2.0 async models + session (`session_scope()`).
- `mono/` — `schemas` (StatementItem), `matcher` (match/candidate/learn), `webhook`
  (`process_statement_item` is bot-agnostic + the FastAPI router), `client` (register).
- `agent/` — `persona` (PLATON_SYSTEM_PROMPT), `provider` (LLMProvider ABC +
  ClaudeCodeProvider + AnthropicAPIProvider stub), `tools` (TOOLS registry),
  `dispatcher` (handle_message: deterministic tool routing).
- `bot/` — aiogram 3 bot, allowlist middleware, text + callback handlers, keyboards,
  and the webhook→Telegram notifier.
- `reminders/` — APScheduler daily payment nudges (Redis jobstore, memory fallback).
- `app.py` — FastAPI; lifespan starts bot long-polling + scheduler + notifier.

## Setup
```bash
python -m venv venv && source venv/bin/activate
uv pip install -e ".[dev]"           # or: pip install -e ".[dev]"
cp .env.example .env                 # fill in tokens; DO NOT add ANTHROPIC_API_KEY
```

## DB / migrations
```bash
alembic upgrade head                 # apply schema (uses DATABASE_URL from settings)
komunalka init-db                    # dev shortcut: create_all (prefer alembic in prod)
komunalka seed-providers             # seed the 6 providers (idempotent)
```
Money is `Decimal` only. All datetimes tz-aware Europe/Kyiv. SQLite returns naive
datetimes on read → normalize with `clock.ensure_aware()` before comparing.

## Patterns (matching)
Seed patterns are TODO placeholders that never match — real mono `description` strings
are captured live. Add them as you see transactions:
```bash
komunalka learn-pattern "Газ (постачання)" "naftogaz"
```
or tap a provider on the bot's categorize prompt (auto-learns the stable token).

## Run
```bash
komunalka register-mono-webhook --dry-run   # inspect the request (token masked)
komunalka register-mono-webhook             # actually register with mono
uvicorn komunalka.app:app --host 0.0.0.0 --port 8000   # webhook + bot polling + scheduler
```
The mono webhook must be reachable over public HTTPS at
`${PUBLIC_BASE_URL}/mono/webhook/${MONO_WEBHOOK_SECRET}`.

## Test, lint, types
```bash
pytest -q                       # 36 tests, in-memory SQLite, no network, no API key
ruff check src tests            # lint (E,W,F,I,UP,B)
ruff format src tests           # format (black-compatible; the project standard)
mypy                            # type-check src/ (config in pyproject)
```
Tests use a fake `LLMProvider` (no real `claude` calls) and pass `now` explicitly to
reminder logic (no time-freezing dependency). Formatting/linting is **Ruff**
(`ruff format` replaces black); type-checking is **mypy** with `ignore_missing_imports`
(aiogram/apscheduler ship partial/no stubs).

## Env vars (see `.env.example`)
`MONO_TOKEN`, `MONO_WEBHOOK_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID`,
`DATABASE_URL`, `REDIS_URL`, `CLAUDE_BIN`, `LLM_PROVIDER` (claude_code|anthropic_api),
`UTILITY_MCCS`, `TZ`, `PUBLIC_BASE_URL`. **Never** `ANTHROPIC_API_KEY`.

## Conventions
- Conventional commits, one logical change each.
- Ukrainian for all user-facing copy; English for code/logs.
- Tool routing is deterministic in Python; the LLM only picks `{tool,args}` and writes
  the `message`. Never let the model fabricate amounts/balances/"paid" status.
- Phase-2 stubs (`submit_meter_reading`, `get_provider_balance`, meter nudges) raise
  `NotImplementedError` / are inactive — do not wire them up in Phase 1.
