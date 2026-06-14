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

![tests](https://img.shields.io/badge/tests-74%20passing-2ea44f)
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
(Phase 1), plus **meter readings** — photo → OCR → delta-validate → submit (Phase 2).
No provider balance scraping, no payment initiation (Phase 3). See [`docs/`](docs/)
for the full spec and [`CLAUDE.md`](CLAUDE.md) for operational detail.

## A day with the butler
> **You:** що треба заплатити?
> **🎩:** Відкрите: вода (≈180 ₴) і ДАХ (суму поки не знаю). Решта оплачена.
>
> _\[you snap a photo of the gas meter\]_
> **🎩:** ✅ Газ — 4827.05. Лишилось передати — SMS на 4647 з текстом «… 4827.05».
>
> **You:** скільки вийшло за травень?
> **🎩:** За травень — 3 920 ₴. Найбільше з'їв газ. Показати графіком?

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
| Tooling | pytest · Ruff (lint + format) · mypy |

## Quick start
```bash
git clone git@github.com:vyahello/dvoretskyi.git && cd dvoretskyi
python -m venv venv && source venv/bin/activate
uv pip install -e ".[dev]"          # or: pip install -e ".[dev]"
cp .env.example .env                # fill in your tokens

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
pytest -q              # 74 tests, in-memory SQLite, no network, no API key needed
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
alembic/  migrations (0001 schema · 0002 meter_readings · 0003 meter_decimals)
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

