# Komunalka — Комунальний Дворецький

A single-user Telegram **utility-butler agent**: it tracks every household utility
payment from the monobank webhook, answers "what's still unpaid?", builds spend
stats, and nudges you before deadlines — all in a dry, deadpan Ukrainian butler voice
(no personal name). Free-text chat is handled by an LLM that returns a structured
`{tool, args, message}` decision; tool execution is deterministic Python.

**Phase 1 scope:** money + stats + reminders + conversational agent over internal
data. No provider scraping, no meter OCR, no payment initiation (those are later
phases). See [`docs/`](docs/) for the full spec and [`CLAUDE.md`](CLAUDE.md) for
operational detail.

## Stack
Python 3.12+ · FastAPI · aiogram 3 · SQLAlchemy 2.0 (async) + Alembic · APScheduler
(Redis jobstore, memory fallback) · pydantic-settings · LLM via headless Claude Code
CLI (`claude -p`). Tooling: pytest · Ruff (lint + format) · mypy.

> ⚠️ **Never set `ANTHROPIC_API_KEY`.** Claude Code prioritizes it over the Max
> subscription and silently bills the API. The bot strips it from the `claude`
> subprocess env; keep it out of `.env` and the service environment.

## Quick start
```bash
python -m venv venv && source venv/bin/activate
uv pip install -e ".[dev]"          # or: pip install -e ".[dev]"
cp .env.example .env                # fill tokens — do NOT add ANTHROPIC_API_KEY

alembic upgrade head                # create schema
komunalka seed-providers            # seed the 6 providers (idempotent)

# register the mono webhook (inspect first, then send)
komunalka register-mono-webhook --dry-run
komunalka register-mono-webhook

# run webhook + Telegram polling + reminder scheduler
uvicorn komunalka.app:app --host 0.0.0.0 --port 8000
```
The mono webhook must be reachable over public HTTPS at
`${PUBLIC_BASE_URL}/mono/webhook/${MONO_WEBHOOK_SECRET}`.

Teach the matcher real payee strings as you see them (or tap a provider on the bot's
categorize prompt, which auto-learns):
```bash
komunalka learn-pattern "Газ (постачання)" "naftogaz"
```

## Commands
Telegram menu (also published in code via `set_my_commands`): `/start`, `/unpaid`
(open this month), `/stats` (this month's spend, as a chart when there's data),
`/help`. Commands run **deterministically** — no LLM. Anything else you type is free
text handled by the agent (e.g. «що треба заплатити?»).

## How it works
- **Webhook** → idempotent (by `mono_tx_id`), outflow-only. Matches the description
  to a provider → logs + confirms; unmatched-but-utility-candidate → asks you to
  categorize (and learns the pattern); non-utility → silently ignored.
- **Agent tools:** `get_unpaid`, `get_stats` (+PNG chart), `log_payment_manual`,
  `categorize_payment`, `snooze_reminder`.
- **Reminders:** daily job nudges unpaid providers inside their due-day window, once
  per cycle, respecting snooze; escalates near the deadline.

## Test & static analysis
```bash
pytest -q              # 36 tests, in-memory SQLite, no network, no API key needed
ruff check src tests   # lint (E,W,F,I,UP,B)
ruff format src tests  # format (black-compatible; project standard)
mypy                   # type-check src/
```
All four are green on a clean tree. Tests use a fake `LLMProvider` (no real `claude`
calls) and pass time explicitly to the reminder logic.

## Repo map
```
src/komunalka/  config·clock·app(FastAPI lifespan)·cli
  db/   models (SQLAlchemy 2.0) + async session
  mono/ schemas · matcher · webhook · client
  agent/ persona (BUTLER_SYSTEM_PROMPT) · provider (LLMProvider) · tools · dispatcher
  bot/  aiogram bot + allowlist + keyboards
  reminders/ APScheduler engine
tests/  conftest + matcher/webhook/tools/dispatcher/reminders
alembic/  migrations
```
