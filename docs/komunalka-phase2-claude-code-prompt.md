# Claude Code — Phase 2 Implementation Prompt: Meter Readings

> Paste into a Claude Code session at the repo root **after Phase 1 is live**.
> Spec: `docs/komunalka-agent-spec.md` (read §3, §5a, §6, §7 first). This prompt is
> the build order and hard contracts for Phase 2.

---

## Role & scope

Add **Phase 2**: meter readings. User sends a **photo of a meter** to the bot →
OCR the value → **validate against history** (delta sanity) → store → submit to the
provider (where a channel exists) or hand back the validated value. Plus
**meter-window reminders** (activate the `kind="meter"` branch scaffolded in Phase 1).

### Meter providers (only these have meters)
- **Газ (постачання)** — submission via gas.ua form **or** SMS 4647
- **Холодна вода (Львівводоканал)** — submission via ВК site form / Viber bot

Electricity is **excluded** — the user does not submit electricity readings.
Internet / ДАХ have no meters.

### In scope (Phase 2a — robust core, build this first)
- Telegram photo handler → OCR pipeline
- Vision OCR via Claude Code (`Read` tool), behind a `VisionProvider` abstraction
- **Delta validation** against last reading (the trust layer)
- `meter_readings` persistence (make the scaffolded model real)
- `SubmissionChannel` abstraction with a safe **`ManualAssistChannel`** default
  (bot returns the validated value + how/where to submit; user sends it)
- Meter-window reminders (activate `kind="meter"`)
- Implement the Phase-1 stubs `submit_meter_reading` / `get_provider_balance`
  (the latter stays a stub if no balance source — see out-of-scope)

### Out of scope (do NOT build)
- **Auto-submission** to gas.ua / ВК as the default. Scaffold `SmsChannel` and
  `WebFormChannel` as **opt-in, feature-flagged, `--dry-run`-first** (Phase 2b),
  but the default channel is `ManualAssistChannel`. Do not reverse-engineer provider
  login/auth; if a channel needs auth, leave it as a documented stub.
- Building an actual SMS gateway / GSM hardware integration (the channel only formats
  the SMS body and, if `SMS_GATEWAY_URL` is set, POSTs to it; otherwise emits an `sms:` deep link).
- Electricity meters; pay-assist (Phase 3); `get_provider_balance` real scraping.

If you find yourself reaching outside this, stop and ask.

---

## Stack additions
Reuse Phase 1 stack. New: Pillow (image handling / downscale before OCR). No new
heavy deps without asking. Vision OCR runs through the existing Claude Code CLI.

---

## Data model (`db/models.py`) — make `meter_readings` real + migration

```
MeterReading
  id: int PK
  provider_id: FK
  cycle: str                  # "YYYY-MM"
  value: Decimal              # validated reading
  ocr_raw: str | None         # what OCR returned, before validation
  consumption_delta: Decimal | None   # value - previous validated value
  photo_ref: str | None       # temp path or stored ref
  status: Enum(ocr_pending, needs_confirm, validated, submitted, rejected, failed)
  created_at: datetime (tz-aware Kyiv)
  submitted_at: datetime | None
```
Alembic migration for the new/updated table. Decimal for readings; tz-aware Kyiv.
Add `Provider.meter_window` usage (gas ≤5, water per ВК) — already in the model;
seed real windows via `cli.py`.

---

## Vision OCR (`agent/vision.py`)

`VisionProvider` ABC: `async def read_meter(image_path: str) -> MeterRead` where
`MeterRead = {value: Decimal | None, raw: str, note: str}`.

`ClaudeCodeVisionProvider`:
- Downscale the photo with Pillow first (long side ≤ ~1600px) to a temp file.
- Invoke `claude -p` with the **`Read` tool enabled** so Claude Code can view the
  image: `claude -p "<prompt>" --allowedTools "Read" --output-format json`.
  Prompt: tell it the image path, to read the meter's digits, ignore decimal/red
  dials unless clearly part of the integer reading, and return **only**
  `{"value": <number|null>, "raw": "<digits as seen>", "note": "<short>"}`.
- **Env hardening (mandatory, same as Phase 1):** strip `ANTHROPIC_API_KEY` from the
  subprocess env.
- Parse defensively (outer `.result` → inner JSON; strip fences; one retry; on
  failure return `value=None` so the pipeline asks the user to retype).
- Verify the CLI can `Read` images in this version (`claude --help`); if image input
  differs, adapt — do not silently assume.

---

## Delta validation (`agent/meters.py`)

`validate(provider, new_value, history) -> Validation` where
`Validation = {ok: bool, status, consumption, reason}`.

Rules (the trust layer — get these right):
- No previous reading → accept as baseline (`validated`), consumption = None.
- `new_value < previous` → unless plausible meter rollover (digits wrap at the meter's
  max), mark `needs_confirm` with a clear reason ("менше за попередній").
- consumption == 0 → `needs_confirm` ("нуль споживання — точно?").
- consumption > `max(absolute_cap, k × median(history))` (configurable `k`, e.g. 3) →
  `needs_confirm` ("стрибок: +X, звичні ~Y").
- Otherwise → `validated`.

`needs_confirm` must surface a butler-voice message asking to confirm or re-photo
**before** any submission. This is what catches OCR misreads (extra digit, 8↔3, etc.).

---

## Submission (`agent/submission.py`)

`SubmissionChannel` ABC: `async def submit(provider, reading) -> SubmitResult`.

- **`ManualAssistChannel` (DEFAULT for all providers):** does not call the provider.
  Returns the validated value + provider-specific instructions / deep link
  (gas → `sms:4647` body or gas.ua URL; water → ВК bot/site). Marks reading
  `validated` (not `submitted`); a follow-up user "відправив" or button sets `submitted`.
- **`SmsChannel` (opt-in, Phase 2b):** formats the SMS body for 4647; if
  `SMS_GATEWAY_URL` set → POST (supports `--dry-run`); else emits `sms:` deep link.
- **`WebFormChannel` (opt-in, experimental, Phase 2b):** POST to gas.ua form; dry-run
  first; documented as fragile; **never** the default.

Channel selection per provider via config (`SUBMISSION_CHANNELS=gas:manual,water:manual`
by default).

---

## Telegram photo handler (`bot/app.py`)

- Allowlisted user sends a photo → download highest-res → temp file → run pipeline.
- **Which meter?** If exactly one meter provider is in its window → assume it. Else
  ask via inline buttons ([Газ] [Вода]). The agent can also route captions like
  "показники газу" + photo.
- Pipeline: OCR → validate → if `needs_confirm`, ask (with [Підтвердити]/[Перефотографувати]);
  if `validated`, store + run submission channel + reply in butler voice.

---

## Tools (extend `agent/tools.py`)
- `submit_meter_reading(provider_name, image_path)` — implement (was stub): full
  OCR→validate→store→submit pipeline; returns status + message.
- `confirm_meter_reading(reading_id)` — user confirms a `needs_confirm` reading → submit.
- `get_meter_history(provider_name)` — recent readings + consumption, for context/stats.
- `get_provider_balance` — leave as stub unless a real source exists.

---

## Reminders (`reminders/engine.py`) — activate meter branch
- Enable the `kind="meter"` branch scaffolded in Phase 1.
- Gas window ≤ 5th, water per ВК schedule. Nudge if no `submitted`/`validated`
  reading for the current cycle, within window, not snoozed, once/day. Butler voice
  ("До 5-го показники газу — кинь фото."). Record `NudgeLog(kind="meter")`.

---

## Config additions (`.env.example`)
`SUBMISSION_CHANNELS=gas:manual,water:manual`, `SMS_GATEWAY_URL=` (empty),
`OCR_MAX_LONG_SIDE=1600`, `DELTA_SPIKE_K=3`, gas/water meter windows.
Still **no `ANTHROPIC_API_KEY`**.

## Security (per spec §8)
- Strip `ANTHROPIC_API_KEY` from the vision subprocess env (mandatory).
- Temp photos: write to a private dir, delete after processing; never log image contents.
- Telegram allowlist still applies to photo handler.
- `WebFormChannel`/`SmsChannel` POST only behind explicit config + dry-run.

---

## Tests (pytest, target ≥80% on core logic)
- `test_vision`: fake `VisionProvider` returns canned `MeterRead`; pipeline handles
  `value=None` (asks to retype) and a valid value.
- `test_meters_validation`: baseline; backwards (→needs_confirm); rollover (→ok);
  zero consumption (→needs_confirm); spike > k×median (→needs_confirm); normal (→validated).
- `test_submission`: `ManualAssistChannel` returns instructions + marks `validated`,
  not `submitted`; `confirm_meter_reading` flips to `submitted`. `SmsChannel` dry-run
  formats body without POSTing.
- `test_photo_handler`: single-meter-in-window auto-routes; two → asks. Fake bot.
- `test_reminders_meter`: meter nudge fires in window, suppressed once submitted/snoozed.

---

## Working conventions
- Order: model+migration → vision → meters(validation) → submission → tools →
  photo handler → reminders(meter) → tests. Each step runnable/testable.
- Conventional commits; one logical change per commit.
- Update `README.md` + `CLAUDE.md` after (per the standing rule): Phase 2 features,
  new env vars, `ManualAssistChannel` default, "no auto-submit by default".
- Ask before adding deps or before touching the out-of-scope list.

## Definition of done
1. A meter photo → OCR returns a value (or asks to retype on failure).
2. A reading lower/zero/huge vs history → bot asks to confirm before doing anything;
   a normal reading → stored as `validated`.
3. `ManualAssistChannel` returns the validated value + how to submit; the reading is
   `validated`, and confirming "відправив" marks it `submitted`.
4. A meter-window day nudges once for an unsubmitted meter, in natural butler voice,
   respecting snooze.
5. `pytest` green; vision subprocess runs with **no `ANTHROPIC_API_KEY`**.
6. No auto-submission happens unless a channel is explicitly enabled in config.
