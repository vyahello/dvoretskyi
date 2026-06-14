# Комунальний Дворецький

<p align="center">
  <img src="assets/dvoretskyi.png" alt="Комунальний Дворецький" width="220">
</p>

A single-user Telegram **utility-butler agent**: it tracks every household utility
payment from the monobank webhook, answers "what's still unpaid?", builds spend
stats, and nudges you before deadlines — all in a dry, deadpan Ukrainian butler voice
(no personal name). Free-text chat is handled by an LLM that returns a structured
`{tool, args, message}` decision; tool execution is deterministic Python.

**Scope:** money + stats + reminders + conversational agent over internal data
(Phase 1), plus **meter readings** — photo → OCR → delta-validate → submit (Phase 2).
No provider balance scraping, no payment initiation (Phase 3). See [`docs/`](docs/)
for the full spec and [`CLAUDE.md`](CLAUDE.md) for operational detail.

## Stack
Python 3.12+ · FastAPI · aiogram 3 · SQLAlchemy 2.0 (async) + Alembic · APScheduler
(Redis jobstore, memory fallback) · pydantic-settings · Pillow · LLM + vision OCR via
headless Claude Code CLI (`claude -p`). Tooling: pytest · Ruff (lint + format) · mypy.

> ⚠️ **Never set `ANTHROPIC_API_KEY`.** Claude Code prioritizes it over the Max
> subscription and silently bills the API. The bot strips it from the `claude`
> subprocess env; keep it out of `.env` and the service environment.

## Quick start
```bash
git clone git@github.com:vyahello/dvoretskyi.git && cd dvoretskyi
python -m venv venv && source venv/bin/activate
uv pip install -e ".[dev]"          # or: pip install -e ".[dev]"
cp .env.example .env                # fill tokens — do NOT add ANTHROPIC_API_KEY

alembic upgrade head                # create schema
dvoretskyi seed-providers            # seed the 6 providers (idempotent)

# register the mono webhook (inspect first, then send)
dvoretskyi register-mono-webhook --dry-run
dvoretskyi register-mono-webhook

# run webhook + Telegram polling + reminder scheduler
uvicorn dvoretskyi.app:app --host 0.0.0.0 --port 8000
```
The mono webhook must be reachable over public HTTPS at
`${PUBLIC_BASE_URL}/mono/webhook/${MONO_WEBHOOK_SECRET}`.

Teach the matcher real payee strings as you see them (or tap a provider on the bot's
categorize prompt, which auto-learns):
```bash
dvoretskyi learn-pattern "Газ (постачання)" "naftogaz"
```

## Commands
Telegram menu (also published in code via `set_my_commands`): `/start`, `/unpaid`
(open this month), `/stats` (this month's spend, as a chart when there's data),
`/help`. Commands run **deterministically** — no LLM. Anything else you type is free
text handled by the agent (e.g. «що треба заплатити?»). Send a **photo of a meter**
to record a reading (gas/water) — the bot routes, OCRs, validates, and tells you how
to submit.

## How it works
- **Webhook** → idempotent (by `mono_tx_id`), outflow-only. Matches the description
  to a provider → logs + confirms; unmatched-but-utility-candidate → asks you to
  categorize (and learns the pattern); non-utility → silently ignored.
- **Agent tools:** `get_unpaid`, `get_stats` (+PNG chart), `log_payment_manual`,
  `categorize_payment`, `snooze_reminder`, `submit_meter_reading`,
  `confirm_meter_reading`, `get_meter_history`.
- **Meters (gas & water):** send a **photo** → OCR (`claude -p --allowed-tools "Read"`)
  → **delta validation** against history (backwards / zero / spike → asks you to
  confirm or re-photo before anything is submitted) → stored. Default submission is
  **`ManualAssistChannel`**: the bot hands back the validated value + how/where to
  submit (gas→SMS 4647/gas.ua, water→ВК) and the **«Відправив ✓»** tap marks it sent.
  **No auto-submission** unless a channel is enabled in `SUBMISSION_CHANNELS`. OCR
  failure → it asks you to retype, never guesses.
- **Reminders:** daily jobs nudge unpaid providers inside their due-day window and
  meters inside their submission window (gas ≤5, water ~25), once per cycle, respecting
  snooze; payment copy escalates near the deadline.

## Test & static analysis
```bash
pytest -q              # 70 tests, in-memory SQLite, no network, no API key needed
ruff check src tests   # lint (E,W,F,I,UP,B)
ruff format src tests  # format (black-compatible; project standard)
mypy                   # type-check src/
```
All four are green on a clean tree. Tests use a fake `LLMProvider` and a fake
`VisionProvider` (no real `claude` calls) and pass time explicitly to the reminder /
window logic.

## Repo map
```
src/dvoretskyi/  config·clock·app(FastAPI lifespan)·cli
  db/   models (SQLAlchemy 2.0) + async session
  mono/ schemas · matcher · webhook · client
  agent/ persona · provider (LLMProvider) · tools · dispatcher
         vision (VisionProvider OCR) · meters (delta validation) · submission (channels)
  bot/  aiogram bot + allowlist + keyboards + photo handler
  reminders/ APScheduler engine (payment + meter nudges)
tests/  conftest + matcher/webhook/tools/dispatcher/reminders + vision/meters/submission/photo
alembic/  migrations (0001 schema, 0002 meter_readings)
```
