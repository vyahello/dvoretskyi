# CLAUDE.md — Комунальний Дворецький (operational notes)

Single-user Telegram utility butler ("Комунальний Дворецький" — no personal name).
**Phase 1 (done):** money + stats + reminders + conversational agent from the monobank
webhook. **Phase 2 (done):** meter readings — photo → OCR → delta-validate → submit
(`ManualAssistChannel` default; no auto-submit) + meter-window reminders. Still **no
payment initiation** (Phase 3), no provider balance scraping. See `docs/` for the spec
and the per-phase build prompts.

## Auth
Auth Claude Code via `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`.

## Layout
- `config.py` — pydantic-settings (`get_settings()`), `.env`-driven.
- `clock.py` — tz-aware Europe/Kyiv helpers; cycle = "YYYY-MM".
- `db/` — SQLAlchemy 2.0 async models + session (`session_scope()`).
- `mono/` — `schemas` (StatementItem), `matcher` (match/candidate/learn), `webhook`
  (`process_statement_item` is bot-agnostic + the FastAPI router), `client` (register).
- `agent/` — `persona` (BUTLER_SYSTEM_PROMPT), `provider` (LLMProvider ABC +
  ClaudeCodeProvider + AnthropicAPIProvider stub), `tools` (TOOLS registry),
  `dispatcher` (handle_message: deterministic tool routing). **L2 meters:** `vision`
  (VisionProvider ABC + ClaudeCodeVisionProvider — `claude -p --allowed-tools "Read"`,
  Pillow downscale, robust JSON extract), `meters` (pure delta `validate` + `window_open`),
  `submission` (SubmissionChannel ABC + `ManualAssistChannel` default + Sms/WebForm
  opt-in).
- `bot/` — aiogram 3 bot, allowlist middleware, slash commands (`/start /unpaid
  /stats /help` — deterministic, registered before the free-text catch-all and
  mirrored via `set_my_commands`), text + callback handlers, **photo handler**
  (`F.photo` → meter pipeline), keyboards, and the webhook→Telegram notifier.
- `reminders/` — APScheduler daily payment **and** meter nudges (Redis jobstore,
  memory fallback).
- `app.py` — FastAPI; lifespan starts bot long-polling + scheduler + notifier.

## Setup
```bash
python -m venv venv && source venv/bin/activate
uv pip install -e ".[dev]"           # or: pip install -e ".[dev]"
cp .env.example .env                 # fill in tokens
```

## DB / migrations
```bash
alembic upgrade head                 # apply schema (uses DATABASE_URL from settings)
dvoretskyi init-db                    # dev shortcut: create_all (prefer alembic in prod)
dvoretskyi seed-providers             # seed the 6 providers (idempotent)
```
Money is `Decimal` only. All datetimes tz-aware Europe/Kyiv. SQLite returns naive
datetimes on read → normalize with `clock.ensure_aware()` before comparing.

## Patterns (matching)
Seed patterns are TODO placeholders that never match — real mono `description` strings
are captured live. Add them as you see transactions:
```bash
dvoretskyi learn-pattern "Газ (постачання)" "naftogaz"
```
or tap a provider on the bot's categorize prompt (auto-learns the stable token).

## Meters (L2, Phase 2)
Send the bot a **photo** of a meter → OCR (`agent/vision.py` via `claude -p
--allowed-tools "Read"`) → **delta validation** (`agent/meters.validate`: backwards /
zero / spike vs history → `needs_confirm`) → store `MeterReading` → submit.
- **Which meter?** Caption hint ("показники газу") → else the single meter provider in
  its window → else ask `[Газ][Вода]` (an `ocr_pending` row holds the photo until the
  tap routes it).
- **Submission is `ManualAssistChannel` by default**: the bot hands back the validated
  value + how/where to submit (gas→SMS 4647/gas.ua, water→ВК), marks the reading
  `validated`; the **«Відправив ✓»** tap (`ms:`) flips it to `submitted`. **No
  auto-submission** unless a channel is explicitly enabled in `SUBMISSION_CHANNELS`.
- Only **gas** and **water** have meters (set `Provider.meter_window`). Electricity /
  internet / housing have none. OCR failure → `value=None` → ask to retype (never guess).
- Temp photos live in a private dir and are deleted right after processing (image bytes
  never logged).

## Run
```bash
dvoretskyi register-mono-webhook --dry-run   # inspect the request (token masked)
dvoretskyi register-mono-webhook             # actually register with mono
uvicorn dvoretskyi.app:app --host 0.0.0.0 --port 8000   # webhook + bot polling + scheduler
```
The mono webhook must be reachable over public HTTPS at
`${PUBLIC_BASE_URL}/mono/webhook/${MONO_WEBHOOK_SECRET}`.

## Test, lint, types
```bash
pytest -q                       # 70 tests, in-memory SQLite, no network, no API key
ruff check src tests            # lint (E,W,F,I,UP,B)
ruff format src tests           # format (black-compatible; the project standard)
mypy                            # type-check src/ (config in pyproject)
```
Tests use a fake `LLMProvider` **and a fake `VisionProvider`** (no real `claude` calls)
and pass `now` explicitly to reminder/window logic (no time-freezing dependency). Formatting/linting is **Ruff**
(`ruff format` replaces black); type-checking is **mypy** with `ignore_missing_imports`
(aiogram/apscheduler ship partial/no stubs).

## Env vars (see `.env.example`)
`MONO_TOKEN`, `MONO_WEBHOOK_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID`,
`DATABASE_URL`, `REDIS_URL`, `CLAUDE_BIN`, `LLM_PROVIDER` (claude_code|anthropic_api),
`UTILITY_MCCS`, `TZ`, `PUBLIC_BASE_URL`. **Meters:** `CLAUDE_VISION_TIMEOUT_SECONDS`
(vision is slower than text), `SUBMISSION_CHANNELS=gas:manual,water:manual`,
`SMS_GATEWAY_URL` (empty → deep link only), `OCR_MAX_LONG_SIDE`, `DELTA_SPIKE_K`,
`DELTA_ABS_CAP`, `GAS_METER_DAY`, `WATER_METER_DAY`.

## Conventions
- Conventional commits, one logical change each.
- Brand/persona: **Комунальний Дворецький** — a butler with **no personal name**
  (never invent one). System prompt = `agent/persona.py::BUTLER_SYSTEM_PROMPT`.
- The 6 seed providers (see `cli.py::SEED_PROVIDERS`): Холодна вода,
  Електроенергія (ЛЕЗ), Газ (постачання), Газ (доставлення), **Інтернет (Gigabit+)**,
  Кварплата (ДАХ).
- Ukrainian for all user-facing copy; English for code/logs.
- Tool routing is deterministic in Python; the LLM only picks `{tool,args}` and writes
  the `message`. Never let the model fabricate amounts/balances/"paid"/meter values.
- Photo/meter taps call tools directly (deterministic), never through the LLM —
  `submit_meter_reading` needs an `image_path` the model never has.
- Still stubbed (raise `NotImplementedError`): `get_provider_balance` (no source —
  spec §9) and `WebFormChannel` live submit (provider auth not reverse-engineered).
  `AnthropicAPIProvider` is a drop-in swap, not yet implemented.
