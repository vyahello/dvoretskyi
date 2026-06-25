<div align="center">

<img src="assets/dvoretskyi.png" alt="Комунальний Дворецький" width="200">

# Комунальний Дворецький

**_«До ваших послуг.»_** &nbsp;A single-user Telegram **utility butler** who tracks your
bills, reads your meters from a photo, and nudges you before the deadline — with the
minimum necessary enthusiasm.

![python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)
![fastapi](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![aiogram](https://img.shields.io/badge/aiogram-3-26A5E4?logo=telegram&logoColor=white)
![sqlalchemy](https://img.shields.io/badge/SQLAlchemy-2.0-D71F00?logo=sqlalchemy&logoColor=white)
![claude](https://img.shields.io/badge/Claude%20Code-CLI-D97757?logo=anthropic&logoColor=white)

![tests](https://img.shields.io/badge/tests-191%20passing-2ea44f)
![lint](https://img.shields.io/badge/ruff-clean-261230?logo=ruff&logoColor=white)
![types](https://img.shields.io/badge/mypy-strict-2a6db2)
![butler](https://img.shields.io/badge/enthusiasm-minimal-1f4d3f)

</div>

He watches your monobank webhook so you don't have to: logs every utility payment,
answers "що треба заплатити?", draws spend charts, and reminds you about deadlines —
all in a dry, deadpan Ukrainian voice (he has **no personal name**; a butler doesn't
need one). Free-text chat goes to an LLM that returns a structured
`{tool, args, message}` decision; the actual work is deterministic Python — the model
never gets to invent your balance.

**Scope:** money + stats + reminders + conversational agent over internal data
(Phase 1), plus **meter readings** — photo → OCR → delta-validate → file to the
[infolviv](https://infolviv.com.ua) portal (Phase 2), **voice notes** (local Whisper in,
local Piper out — a voice ask gets a spoken reply),
and **two properties** (a lived-in home + an unoccupied flat) tracked separately or
together. Live **balance scraping** for the internet & mobile cabinets; still **no
payment initiation** (Phase 3). See [`docs/`](docs/) for the full spec and
[`CLAUDE.md`](CLAUDE.md) for operational detail.

## A day with the butler
> **You:** що треба заплатити?
> **🎩:** Відкрите: вода (≈180 ₴) і ДАХ (суму поки не знаю). Решта оплачена.
>
> _\[you snap a photo of the gas meter\]_
> **🎩:** 💨 Газ (постачання): записав 4827.05 (намотало +42). Подати на портал? _\[📤 Подати\]_
>
> **You:** скільки за газ на другому житлі?
> **🎩:** _\[a spend table\]_ 📊 Газ · Житло 2 · травень — разом 1 240 ₴
>
> **You:** _\[voice\]_ скільки вийшло за травень?
> **🎩:** _\[voice reply\]_ За травень — 3 920 ₴. Найбільше з'їв газ. _\[chart\]_

## Stack
| Area | Tech |
|---|---|
| Language | Python 3.12+ |
| Web / webhook | FastAPI |
| Telegram | aiogram 3 |
| Database | SQLAlchemy 2.0 (async) + Alembic |
| Scheduler | APScheduler (Redis jobstore, memory fallback) |
| Config | pydantic-settings |
| Images | Pillow |
| LLM + vision OCR | headless Claude Code CLI (`claude -p`) |
| Voice STT | faster-whisper (CTranslate2, in-process — no daemon) |
| Voice TTS | Piper (local binary → ffmpeg OGG/Opus) — replies to voice asks |
| Tooling | pytest · Ruff (lint + format) · mypy |

## Quick start
```bash
git clone git@github.com:vyahello/dvoretskyi.git && cd dvoretskyi
python -m venv venv && source venv/bin/activate
uv pip install -e ".[dev]"          # or: pip install -e ".[dev]"
cp .env.example .env                # fill in your tokens

alembic upgrade head                # create schema
dvoretskyi seed-providers            # seed the 7 providers (idempotent)

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
text handled by the agent (e.g. «що треба заплатити?»). For a typed ask, once the butler
knows what you want it sends a short natural «I'm on it» line («зазираю в кабінет
інтернету…») and then the answer — like a real assistant, never echoing your words back.
(On a **voice** ask that line is skipped — the «записує аудіо…» header already says it's
working — and the answer comes straight back as a voice note.) Send a **photo of a
meter** to record a reading (gas/water) —
the bot routes, OCRs, validates, and tells you how to submit. You can also send a
**voice note**: it's transcribed locally (faster-whisper, on-box — audio is deleted
right after) and handled exactly like a typed message. Meter values stay photo-only
(speech misreads digits).

## How it works
- **Webhook** → idempotent (by `mono_tx_id`), outflow-only. Matches the description
  to a provider → logs + confirms; unmatched-but-utility-candidate → asks you to
  categorize (and learns the pattern); non-utility → silently ignored.
- **Agent tools:** `get_unpaid`, `get_stats` (+PNG chart), `log_payment_manual`,
  `categorize_payment`, `snooze_reminder`, `submit_meter_reading`,
  `confirm_meter_reading`, `delete_meter_reading`, `get_meter_history`,
  `get_meter_photo`, `get_provider_balance`. The LLM only picks `{tool, args}` and writes
  the reply copy; the work is deterministic Python — it never invents an amount or value.
- **Provider balance (Gigabit+ & mobile):** `get_provider_balance` logs into the ISP
  cabinet (`cabinet.gigabit.te.ua`, JSON API) and reads the current balance, last top-up,
  **monthly fee** (from the tariff plan, not hardcoded), and the **contract/login** — so
  «нагадай мій логін / яка абонплата» are answered from the cabinet, never the model's
  memory. Below the monthly fee → "треба поповнити" with a tappable **«💳 Поповнити N ₴»**
  button (a Portmone deep link with the contract + amount pre-filled, built from env —
  no card data ever touches the bot); above → "не треба, останнє поповнення …". Cached
  ~1h. Credentials live only in the VPS `.env`.
- **Meters (gas & water):** send a **photo** → OCR (`claude -p --allowed-tools "Read"`)
  → **delta validation** against history (backwards / zero / spike → asks you to confirm
  or re-photo before anything is filed) → **stored, never filed on the spot**. Filing is
  **date-gated**: inside the **28→month-end** window the reply offers a one-tap approve
  («📤 Подати») that files the value to the [infolviv](https://infolviv.com.ua) portal
  (`setMultipleFactors`); before the window it offers «подай раніше» — it resists twice
  and files on the third insistence. A fresh photo of a meter **supersedes** that meter's
  earlier unfiled draft, so the journal never piles up duplicates. The **«Мої показники»**
  button merges the authoritative portal record with any unfiled photo drafts that are
  ahead of it. `INFOLV_SUBMIT_ENABLED` is a **kill-switch** — off ⇒ the bot falls back to
  handing you the value + a **«Відправив ✓»** tap. Each photo is **archived** (downscaled
  JPEG in a private dir) so «витягни фото газу» pulls it back; OCR failure → it asks you
  to retype, never guesses.
- **Two properties (home + flat):** providers, payments and meters belong to a
  **household**. monobank's webhook can't tell the two apart, so shared utilities
  (Електроенергія, Газ доставлення) **default to home** and you re-point the rare flat
  payment with a **one-tap «↪ Це <житло>»**; every confirmation names the property it
  landed under. Stats are **combined by default**, or scoped — «скільки за газ на
  <адресі>» filters to one property's gas, «статистика по житлах» splits the total. The
  flat is unoccupied, so its gas meter is a **static** value the month-end nudge offers to
  file with a tap (no photo). **Addresses, account codes and the static value live only in
  the VPS `.env`** — code and tests use neutral slugs (`primary`/`secondary`, «Житло 1/2»).
- **Voice notes:** send a **voice message** → it's transcribed **locally** and handled
  exactly like a typed message. Whisper here is **not a server** — `faster-whisper` is a
  Python library that runs **in the bot's own process** (CTranslate2, a C++ inference
  engine with Python bindings); no daemon, no HTTP, no port. The model weights download
  once to a local cache and load lazily into RAM on the first voice note (cached for the
  process lifetime); inference runs off the event loop (`asyncio.to_thread`) with a
  timeout so the bot never blocks. Audio is deleted right after; bytes are never logged.
  Rather than echoing your words back, once the agent picks a tool the bot sends a short,
  natural, topic-aware «I'm on it» line («Зазираю в кабінет інтернету…», «Підіймаю
  показники газу…») and the answer then carries just the data. Meter *values* stay
  photo-only (speech misreads digits); destructive actions keep their confirm-tap.
  Kill-switch: `STT_PROVIDER=none`.
- **Voice replies:** a voice ask is **answered by voice**. The agent's reply is
  synthesized on-box by **Piper** (a local neural-TTS binary, like the `claude` CLI — no
  pip dep, audio never leaves the server), re-encoded to OGG/Opus with ffmpeg, and sent as
  a Telegram voice note (buttons ride on it; a chart/photo is still attached). Screen text
  is cleaned for speech first (`voiceify`: emoji/markup dropped, «₴» → «гривень», meter
  readings → «… кубометра», a period → «червень дві тисячі двадцять шостого року»). Word
  stress, which espeak's uk rules sometimes get wrong, is corrected by an espeak
  pronunciation dictionary (`scripts/build_espeak_stress.py` compiles
  `scripts/uk_stress_overrides.txt` into a custom espeak data dir Piper is pointed at — no
  text trick works, that's verified). It
  **falls back to text** whenever synth can't run — disabled, no voice model installed
  (`PIPER_VOICE` empty), reply too long, or any error — so a voice asker is never left
  empty-handed. Kill-switch: `TTS_PROVIDER=none`.

  ```
  voice note (OGG/Opus)
        │  aiogram  @router.message(F.voice)
        ▼
  _download_voice ──► private tmp dir (deleted in `finally`)
        │
        ▼
  get_transcription_provider().transcribe(path)
        │   WhisperTranscriptionProvider  (faster-whisper, IN-PROCESS)
        │   • model loaded once, cached on the class
        │   • asyncio.to_thread + timeout  (CPU-bound, off the loop)
        ▼
  transcript ──► _respond_to_text(transcript)   ◄── SAME path as typed text
        │
        ▼
  agent_dispatcher.handle_message
        │   LLM (Claude) returns JSON {tool, args, message}
        │   tool picked? → on_progress: "Зазираю в кабінет інтернету…"  (no echo)
        │   Python runs the tool deterministically (TOOLS registry)
        ▼
  reply text = just the data  (+ pay button / delete-confirm tap / PNG chart)
        │
        ▼   (voice ask only)
  get_tts_provider().synthesize(reply)   ── Piper → ffmpeg → OGG/Opus
        │   • voiceify: emoji/markup out, «₴» → «гривень»
        │   • no model / too long / error → None → send text instead
        ▼
  answer_voice(ogg)  ──► spoken reply (deleted after send)
  ```
- **Reminders:** daily jobs nudge for (1) **payments** inside the due-day window
  (escalating near the deadline), (2) **meters** inside the submission window (the last
  `meter_window` days of the month), and (3) a **low Gigabit+ balance** (below the
  monthly fee). Each fires once per day, respecting **snooze**. Nudges carry a tappable
  **pay button** to the right place: utilities → the **monobank** app, Кварплата → the
  **ДАХ** app, Gigabit+ → its prefilled **Portmone** top-up (iOS App Store / universal
  links; no card data touches the bot). Mobile is **auto-paid** (a scheduled monobank
  payment) so it has no reminder — top-ups still arrive via the webhook, and a manual
  top-up link is available on request.

## Test & static analysis
```bash
pytest -q              # 204 tests, in-memory SQLite, no network, no API key needed
ruff check src tests   # lint (E,W,F,I,UP,B)
ruff format src tests  # format (black-compatible; project standard)
mypy                   # type-check src/
```
All four are green on a clean tree. Tests use a fake `LLMProvider`, a fake
`VisionProvider`, a fake `TranscriptionProvider` and a fake `TTSProvider` (no real
`claude`/Whisper/Piper calls; faster-whisper is imported lazily so the suite never loads
it, and TTS never shells out to the binary) and pass time explicitly to the reminder /
window logic.

## Repo map
```
src/dvoretskyi/  config·clock·app(FastAPI lifespan)·cli
  db/   models (SQLAlchemy 2.0) + async session
  mono/ schemas · matcher · webhook · client
  agent/ persona · provider (LLMProvider) · tools · dispatcher · balance (cabinet scrape)
         vision (VisionProvider OCR) · meters (delta validation) · infolviv (portal filing)
         submission (legacy channels) · photo_store (archive) · transcription (Whisper STT)
         tts (Piper TTS — voice replies)
  households (slug helpers — addresses stay in env, never code)
  bot/  aiogram bot + allowlist + keyboards + photo & voice handlers + webhook notifier
  reminders/ APScheduler engine (payment + meter + balance nudges)
tests/  conftest + matcher/webhook/tools/dispatcher/reminders + vision/meters/submission/photo
alembic/  migrations (0001 schema · 0002 meter_readings · 0003 meter_decimals · 0004 households)
```

## Deploy & ops
Runs on an Ubuntu 24.04 VPS behind nginx + Let's Encrypt as a `systemd` service on
`127.0.0.1:8100`. CI/CD (`.github/workflows/ci.yml`) runs ruff + mypy + pytest on every
push/PR to `main`, then SSH-deploys on green. The deployment
[architecture](deploy/README.md#architecture-deployment-overview), required secrets,
full setup, and a **[troubleshooting guide](deploy/README.md#troubleshooting)** live in
[`deploy/README.md`](deploy/README.md).

---

<div align="center"><sub>Built for one household and one butler. He'll let you know if the gas is overdue. 🎩</sub></div>

