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
  `dispatcher` (handle_message: deterministic tool routing; takes a short `history` of
  recent turns → `context["recent_dialogue"]` so the model resolves short replies like
  «давай»/«а за травень?» against its own last line). **Tool replies must surface data:**
  tools that compute numbers return a `message` the dispatcher appends — `get_stats` now
  renders an **itemised** summary (`_stats_summary`: «📊 <період у словах> — разом X ₴»
  + a bullet per provider with its share; amounts space-grouped via `_fmt_uah`) and the
  two **gas providers stay split** (постачання vs доставлення — no merged «Газ» block).
  It also answers seasons «зима/літо» as 3-month ranges via `_period_bounds`/`_period_label`,
  so a conversational stats ask never dead-ends on a «зараз гляну» preamble.
  `delete_meter_reading` removes a wrongly-entered reading. **L2 meters:** `vision`
  (VisionProvider ABC + ClaudeCodeVisionProvider — `claude -p --allowed-tools "Read"`,
  Pillow downscale, robust JSON extract), `meters` (pure delta `validate` + `window_open`),
  `submission` (SubmissionChannel ABC + `ManualAssistChannel` default + Sms/WebForm
  opt-in). **L2.5 voice:** `transcription` (TranscriptionProvider ABC +
  `WhisperTranscriptionProvider` — local faster-whisper, model cached on the class &
  loaded lazily, runs off-loop via `asyncio.to_thread` with a timeout; `Null…` when
  `STT_PROVIDER=none`). Contract on any failure: empty string → bot asks to retype.
- `bot/` — aiogram 3 bot, allowlist middleware, slash commands (`/start /unpaid
  /stats /help` — deterministic, registered before the free-text catch-all and
  mirrored via `set_my_commands`), text + callback handlers, **photo handler**
  (`F.photo` → meter pipeline), **voice handler** (`F.voice` → transcribe →
  `_respond_to_text` = the same agent path as text), keyboards, and the webhook→Telegram
  notifier. `_respond_to_text` (text + voice) wires an `on_progress` line so the bot says
  a natural «I'm on it» before acting, never echoing the request back.
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
dvoretskyi seed-providers             # seed the 7 providers (idempotent)
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
- **Date-gated submission to infolviv** (the photo flow): a photo is OCR'd, validated and
  **stored** (`auto_submit=False`), never filed on the spot. Filing is gated on the day of
  month (`meters.submit_now`, `meter_submit_from_day=28`): inside the **28→month-end**
  window the reply offers an approve tap (`sf:<rid>`) → one tap files it; **before** the
  window it offers «подай раніше» (`se:<rid>:<attempt>`) which **resists twice and files
  on the 3rd** insistence. Filing calls `infolviv.submit_infolviv_reading(kind, value)`
  (`setMultipleFactors` → `POST /counter/factor`, body verified:
  `[{"factor":"<value>","factorTypeCode":"","counterId":<int>}]`). `INFOLV_SUBMIT_ENABLED`
  is a **kill-switch** — when off the call raises `InfolvivSubmitDisabled` and the bot
  **falls back to manual filing** (hand back the value + «Відправив ✓» `ms:` tap). The
  window label is `28–{last-day}` computed from the calendar (handles 28/29/30/31).
- **«Мої показники» (`menu_meters`) merges two sources:** the infolviv portal record
  (authoritative, filed) **and** `_drafts_block` — photo readings stored but not yet
  filed (`validated`/`needs_confirm`). Portal unreachable → fall back to `_local_journal`.
- **One draft per meter:** a fresh photo of a meter **supersedes** that meter's previous
  un-filed draft (`_supersede_pending` hard-deletes earlier non-`submitted` readings of
  the same provider when a new one is stored). So the journal never piles up duplicates,
  and the freshest is what gets submitted. `submitted` readings are the permanent record
  — never superseded. `delete_meter_reading(provider_name?, cycle?)` is **precisely
  scopeable**: «видали всі» → no scope (wipe all drafts); «видали показник газу» →
  provider; «видали газ за минулий місяць» → provider + `cycle="YYYY-MM"`. It always
  confirms first; `confirm_scope` is packed by `_encode_scope` ('all'|'<pid>'|'<pid|*>:
  <cycle>') and decoded in `execute_meter_delete`. Submitted readings are still refused.
- `_format_unpaid` phrasings (all-clear / mobile-autopay note) are **randomized** so the
  deterministic `/unpaid` reply never reads like a canned autoreply.
- Legacy per-provider `SubmissionChannel`s (`ManualAssistChannel` default, Sms/WebForm
  opt-in via `SUBMISSION_CHANNELS`) still exist for the `auto_submit=True` path, but the
  bot's photo flow now routes everything through the infolviv date-gate above.
- Only **gas** and **water** have meters (set `Provider.meter_window`). Electricity /
  internet / housing have none. OCR failure → `value=None` → ask to retype (never guess).
- Temp photos live in a private dir and are deleted right after processing (image bytes
  never logged).

## Voice (L2.5)
Send the bot a **voice note** → `F.voice` handler downloads the OGG to the private media
dir, transcribes it locally (faster-whisper via `agent/transcription.py`; ffmpeg decodes
Opus), then feeds the transcript into `_respond_to_text` — the **exact same agent path** as
a typed message, so stats/unpaid/balance/deletes all work for free. **No verbatim echo** of
the user's words: instead, once the agent picks a tool the bot sends a short, natural,
topic-aware «I'm on it» line (`dispatcher._progress_line` via the `on_progress` callback —
«Зазираю в кабінет інтернету…», «Підіймаю показники газу…»; varied, deterministic). This
progress line fires for **both typed and voiced** asks (`_respond_to_text` always wires
`on_progress`). When a progress line is sent the reply carries **just the data** (no «зараз
гляну» preamble to double it). A plain chat reply (no tool) just answers — no progress
line. The audio file is
deleted right after (transient; bytes never logged). Empty/failed transcript → «не розчув,
напиши текстом». **Meter values stay photo-only** — STT misreads digits, so a voice turn
can ask or act but never files a reading; destructive actions (delete) keep their confirm-tap.

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
pytest -q                       # 160 tests, in-memory SQLite, no network, no API key
ruff check src tests            # lint (E,W,F,I,UP,B)
ruff format src tests           # format (black-compatible; the project standard)
mypy                            # type-check src/ (config in pyproject)
```
Tests use a fake `LLMProvider`, a fake `VisionProvider` **and a fake
`TranscriptionProvider`** (no real `claude`/Whisper calls; faster-whisper is imported
lazily so the suite never loads it) and pass `now` explicitly to reminder/window logic
(no time-freezing dependency). Formatting/linting is **Ruff**
(`ruff format` replaces black); type-checking is **mypy** with `ignore_missing_imports`
(aiogram/apscheduler ship partial/no stubs).

## Env vars (see `.env.example`)
`MONO_TOKEN`, `MONO_WEBHOOK_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID`,
`DATABASE_URL`, `REDIS_URL`, `CLAUDE_BIN`, `LLM_PROVIDER` (claude_code|anthropic_api),
`UTILITY_MCCS`, `TZ`, `PUBLIC_BASE_URL`. **infolviv:** `INFOLV_LOGIN`, `INFOLV_PWD`,
`INFOLV_SUBMIT_ENABLED` (default false — live POST stays off until the body is verified).
**Meters:** `CLAUDE_VISION_TIMEOUT_SECONDS` (vision is slower than text),
`SUBMISSION_CHANNELS=gas:manual,water:manual`, `SMS_GATEWAY_URL` (empty → deep link only),
`OCR_MAX_LONG_SIDE`, `DELTA_SPIKE_K`, `DELTA_ABS_CAP`, `METER_WINDOW_DAYS`,
`METER_SUBMIT_FROM_DAY` (28), `METER_EARLY_SUBMIT_ATTEMPTS` (3).
**Voice:** `STT_PROVIDER` (whisper|none), `WHISPER_MODEL` (small default; base to save
RAM), `WHISPER_COMPUTE_TYPE` (int8), `WHISPER_LANGUAGE` (uk), `STT_TIMEOUT_SECONDS`.

## Conventions
- Conventional commits, one logical change each.
- Brand/persona: **Комунальний Дворецький** — a butler with **no personal name**
  (never invent one). System prompt = `agent/persona.py::BUTLER_SYSTEM_PROMPT`.
- The 7 seed providers (see `cli.py::SEED_PROVIDERS`): Холодна вода,
  Електроенергія (ЛЕЗ), Газ (постачання), Газ (доставлення), **Інтернет (Gigabit+)**,
  Кварплата (ДАХ), Мобільний. Мобільний hits the webhook via «Поповнення мобільного»
  (telecom MCC), not «Комуналка» → categorize-and-learn like any unmatched tx.
- Ukrainian for all user-facing copy; English for code/logs.
- Tool routing is deterministic in Python; the LLM only picks `{tool,args}` and writes
  the `message`. Never let the model fabricate amounts/balances/"paid"/meter values.
- Photo/meter taps call tools directly (deterministic), never through the LLM —
  `submit_meter_reading` needs an `image_path` the model never has.
- Still stubbed (raise `NotImplementedError`): `get_provider_balance` (no source —
  spec §9) and `WebFormChannel` live submit (provider auth not reverse-engineered).
  `AnthropicAPIProvider` is a drop-in swap, not yet implemented.
