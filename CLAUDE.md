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
  ClaudeCodeProvider — invokes `claude -p` with `--model CLAUDE_MODEL` so the decision
  turn runs on a fast model, not the CLI's heavy default — + AnthropicAPIProvider stub),
  `tools` (TOOLS registry),
  `dispatcher` (handle_message: deterministic tool routing; takes a short `history` of
  recent turns → `context["recent_dialogue"]` so the model resolves short replies like
  «давай»/«а за травень?» against its own last line). `build_context` carries each
  provider's **`due_day`** (day of month due; null = no scheduled payment) + the
  **`autopay_day`**, so «коли треба платити» is answered from real data (the model used to
  wrongly say it «не зберігає дат» — it had no such field in context). **Tool replies must surface data:**
  tools that compute numbers return a `message` the dispatcher appends — `get_stats`
  renders a **modern data table** PNG (`_render_table`: header band with period + grand
  total, zebra rows, a mini share-bar per row, right-aligned СУМА/ЧАСТКА columns; amounts
  space-grouped via `_fmt_uah`). The `message` is then **just a one-line caption**
  (`_stats_caption`: «📊 <період у словах> — разом X ₴») — the per-provider breakdown lives
  in the table, never duplicated in text. `_stats_summary` (itemised bullets) survives only
  as the chartless fallback (no matplotlib). The two **gas providers stay split**
  (постачання vs доставлення — no merged «Газ» block).
  It also answers seasons «зима/літо» as 3-month ranges via `_period_bounds`/`_period_label`,
  so a conversational stats ask never dead-ends on a «зараз гляну» preamble.
  **Payments — dates & plan (distinct from stats):** `get_payment_journal(provider_name?,
  household?, period?)` is the dated per-payment timeline (each `Payment.paid_at` newest-
  first, grouped by provider, capped at 6/provider) — «коли я платив за газ», the data
  `get_stats` (totals/chart) lacks. `get_payment_plan(household?)` is the monthly plan: per
  scheduled provider the **due day**, typical **amount** (`expected_amount`) and **how/where**
  (`balance.pay_method_label`, mirroring `pay_link_for`'s real routing) + deduped pay `links`;
  mobile (`due_day=None`) is listed as a no-action autopay note. Both build their reply in
  `result["message"]` (dispatcher surfaces that); `links` ride as inline buttons
  (`_respond_to_text` → `keyboards.links_keyboard`).
  `delete_meter_reading` removes a wrongly-entered reading. **L2 meters:** `vision`
  (VisionProvider ABC + ClaudeCodeVisionProvider — `claude -p --allowed-tools "Read"`,
  Pillow downscale, robust JSON extract), `meters` (pure delta `validate` + `window_open`),
  `submission` (SubmissionChannel ABC + `ManualAssistChannel` default + Sms/WebForm
  opt-in). **L2.5 voice:** `transcription` (TranscriptionProvider ABC +
  `WhisperTranscriptionProvider` — local faster-whisper, model cached on the class &
  loaded lazily, runs off-loop via `asyncio.to_thread` with a timeout; `Null…` when
  `STT_PROVIDER=none`). Contract on any failure: empty string → bot asks to retype.
  **L2.6 voice-out:** `tts` (TTSProvider ABC + `PiperTTSProvider` default — local Piper
  external binary → WAV → ffmpeg → OGG/Opus — + `Null…` when `TTS_PROVIDER=none`; plus
  `voiceify`: screen text → clean spoken Ukrainian). Contract on any failure: None → the
  bot replies in text.
- `bot/` — aiogram 3 bot, allowlist middleware (`AllowlistMiddleware` admits the
  **owner + any family** — `Settings.allowed_user_ids`, owner ∪ `telegram_extra_allowed_user_ids`),
  slash commands (`/start /unpaid
  /stats /help` — deterministic, registered before the free-text catch-all and
  mirrored via `set_my_commands`; they still work but the **persistent reply keyboard**
  is the primary surface and `HELP_TEXT` steers to it + natural language, not «/команди»),
  text + callback handlers, **photo handler**
  (`F.photo` → meter pipeline), **voice handler** (`F.voice` → transcribe →
  `_respond_to_text` = the same agent path as text), keyboards, and the webhook→Telegram
  notifier. `_respond_to_text` (text + voice) wires an `on_progress` line so the bot says
  a natural «I'm on it» before acting, never echoing the request back. The reply keyboard
  (`keyboards.main_keyboard`) is seven buttons: 💸 Що сплатити · 📊 Статистика /
  🔢 Мої показники · 📜 Історія / 🗓 Як платити · 🌐 Баланс інтернету / ❓ Довідка (the
  low-value «🎩 Привіт» greeting button was dropped — a typed «привіт» is just handled by
  the LLM). **«📜 Історія» (`menu_history`) is a chooser, not a wall of text:** it sends a
  2-button inline menu (🔢 Показники · 💸 Платежі); `on_history_nav` (`h:<view>[:<slug>]`
  callbacks) edits the message in place between the root menu, the readings journal
  (`get_meter_journal` + 📸 photo buttons), and payments (`get_payment_journal`) — payments
  are **split per household** (a `🏠 <житло>` chooser when there are two, since the combined
  list is long), and every leaf carries a **«⬅️ Назад»** button (`keyboards.history_*`).
  **Provider display order** (journal, payment history, plan) is `_provider_order_key`:
  gas, water, electricity, housing, internet, mobile (home household first) — the order the
  user reads bills in, not insertion/due-day. **«🗓 Як платити» (`menu_payplan`)** renders
  `get_payment_plan` — per service the due day, typical amount and **through which service**
  it's paid (monobank «Комуналка» / застосунок ДАХ / Portmone for Gigabit+), with
  `keyboards.links_keyboard` pay-link buttons (deduped by url). The Gigabit+ top-up button
  is **🌐 Поповнити** (not 💳; mobile keeps 💳). `HELP_TEXT` + `cmd_start` **emphasise
  voice** up front (🎙 «можна ГОЛОСОМ»).
- `reminders/` — APScheduler daily payment **and** meter nudges (Redis jobstore,
  memory fallback). The payment nudge fires inside `due_day − payment_nudge_window_days …
  due_day` (**default 5 days**, was 3) and carries a pay link routed by provider type
  (`agent.balance.pay_link_for`: monobank / ДАХ / Portmone), so every reminder says where
  to pay. `PendingNudge.message` names the due day and points at the button.
  **Recipients differ by nudge kind:** payment + balance nudges go to the **owner alone**
  (`telegram_allowed_user_id`); the **«кинь фото» meter nudge is broadcast to the whole
  allowlist** (owner ∪ family — `settings.allowed_user_ids`) since anyone admitted may
  submit a reading. The **static-meter approve tap** (secondary, unoccupied property)
  files one staged row in one tap → single-actor, **owner-only** (so two people can't file
  it twice).
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
zero / spike vs history → `needs_confirm`) → store `MeterReading` → submit. The OCR
prompt makes the model **count the integer wheels and read every leading digit** (a live
misread filed 108.679 as 14.679 by dropping the leading «10»). The validation `history`
is **seeded with the infolviv portal's last filed value** (`_portal_baseline_value`,
routed to the right counter by household account) when it's higher than (or absent from)
our local journal — so a backwards misread is caught against what's actually filed on the
portal even on a clean local DB, not only against local drafts (meters are monotonic → the
highest known prior reading is the true «previous»). Best-effort: portal unreachable →
local history alone. **OCR consensus:** each photo is read **`OCR_READ_ATTEMPTS` times
concurrently** (default 2, same wall-clock) and a value is trusted only when the
independent reads **agree** (`vision._reconcile`); a disagreement (e.g. 108.679 vs
148.679 — a plausible +40 the spike check can't catch) keeps the first value but marks the
`MeterRead` **not `confident`** (the differing read rides in `alt_value`), and
`submit_meter_reading` then forces `needs_confirm` regardless of the delta verdict, asking
the user to verify the number against the meter or re-photograph. This is the real catch
for intermittent single-digit misreads that land in a believable range. **Hint-guided
re-read:** when a baseline exists (portal value, else the last local reading),
`submit_meter_reading` re-OCRs the photo **with the previous value as an anchor**
(`vision.read_meter(path, hint=…)` → `_HINT_TMPL`) and that context-aware read supersedes
the blind one. Knowing the meter stood at ~108 lets the model resolve an ambiguous wheel
(a rounded 0 misread as 4 → 148) the way a human does; the prompt forbids forcing a
clearly-different digit, so a genuine jump still reads true. Skipped when there's no
baseline or no vision provider passed (so the no-`.env` test suite is unaffected).
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
  filed (`validated`/`needs_confirm`). `_drafts_block(portal)` shows a draft **only when
  it's ahead of the portal**: it drops any draft whose month is already filed on the
  portal for that meter (matched by `(kind, household)`, with the gas account → household
  map + primary fallback) — a draft equal to what's filed is just noise. Portal
  unreachable → fall back to `_local_journal`.
- **One draft per meter:** a fresh photo of a meter **supersedes** that meter's previous
  un-filed draft (`_supersede_pending` hard-deletes earlier non-`submitted` readings of
  the same provider when a new one is stored). So the journal never piles up duplicates,
  and the freshest is what gets submitted. `submitted` readings are the permanent record
  — never superseded. `delete_meter_reading(provider_name?, cycle?)` is **precisely
  scopeable**: «видали всі» → no scope (wipe all drafts); «видали показник газу» →
  provider; «видали газ за минулий місяць» → provider + `cycle="YYYY-MM"`. It always
  confirms first; `confirm_scope` is packed by `_encode_scope` ('all'|'<pid>'|'<pid|*>:
  <cycle>') and decoded in `execute_meter_delete`. Submitted readings are still refused.
- **Conversational history pulls from the portal:** `get_meter_history` consults infolviv
  (`reading_for_kind`) by default — the filed value leads (authoritative) + un-filed photo
  drafts after it — so «покажи показники газу» mirrors the «Мої показники» button.
  `use_portal=False` keeps it local-only (the portal-down fallback `_local_journal` passes
  it). Reply text is built tool-side in `result["message"]` (the dispatcher surfaces only
  that), so the readings reach the user instead of a bare «зараз гляну».
- **Month-by-month journal with filing dates** (the «📜 Історія» button / «коли я подавав
  газ?»): `get_meter_journal(provider_name?)` reads **our own `MeterReading` rows** — the
  only place with the full per-month timeline **and `submitted_at`** (the portal returns
  just the latest factor). Covers every metered provider across **both** households
  (the secondary static gas included — unlike `_meter_providers`, which is primary-only),
  newest-first, one entry per cycle (a `submitted` reading wins over a later un-filed
  re-photo of that month), each line «<місяць> — <показник> (спожито …) · подано dd.mm |
  чернетка» with a 📸 mark where the archived photo still exists. The bot handler
  `menu_history` renders `result["message"]`; it's also an LLM tool so «покажи історію
  показників» works conversationally. `get_meter_history` (current state) and
  `get_meter_journal` (timeline + dates) are distinct on purpose. Each journal reading
  carries its `id`; `menu_history` builds a «📸 Фото» **inline button** per month that
  still has an archived photo (`_journal_photo_buttons` → `keyboards.meter_photo_keyboard`,
  `mp:<reading_id>`). The `mp:` tap (`on_meter_photo`) sends **that exact** reading's photo
  via `get_meter_photo_by_id` — a deterministic tap, not via the LLM (mirrors the other
  meter taps). `get_meter_photo` (freshest by provider/cycle) and `get_meter_photo_by_id`
  (one known reading) share the caption builder `_photo_result`.
- `_format_unpaid` phrasings (all-clear / mobile-autopay note) are **randomized** so the
  deterministic `/unpaid` reply never reads like a canned autoreply.
- Legacy per-provider `SubmissionChannel`s (`ManualAssistChannel` default, Sms/WebForm
  opt-in via `SUBMISSION_CHANNELS`) still exist for the `auto_submit=True` path, but the
  bot's photo flow now routes everything through the infolviv date-gate above.
- Only **gas** and **water** have meters (set `Provider.meter_window`). Electricity /
  internet / housing have none. OCR failure → `value=None` → ask to retype (never guess).
- The **temp download** of a photo is deleted right after OCR, but a **compressed copy
  is archived** (`agent/photo_store.py`: downscale to `METER_PHOTO_MAX_LONG_SIDE`,
  re-encode JPEG `METER_PHOTO_QUALITY`, into the private `METER_PHOTO_DIR` / default
  `~/.dvoretskyi/meter_photos`, 0o700) and its path stored in `MeterReading.photo_ref`,
  so the user can pull a meter's photo back later. The archive copy follows the reading:
  superseded/deleted readings have their photo removed (`photo_store.remove`, guarded to
  the archive dir); **submitted** readings keep theirs (permanent record). Image bytes are
  never logged. `get_meter_photo(provider_name?, cycle?)` locates the freshest archived
  photo (filtered by meter/cycle) and returns its path + a caption naming the **household**
  («📸 Газ (постачання) · <житло> — 1888.14 (червень 2026)»); the bot sends the file
  (`_respond_to_text` → `answer_photo` when `tool_result.photo_path` is set), so
  «витягни фото газу» replies with the image, not text.

## Voice (L2.5)
Send the bot a **voice note** → `F.voice` handler downloads the OGG to the private media
dir, transcribes it locally (faster-whisper via `agent/transcription.py`; ffmpeg decodes
Opus), then feeds the transcript into `_respond_to_text` — the **exact same agent path** as
a typed message, so stats/unpaid/balance/deletes all work for free. **No verbatim echo** of
the user's words: instead, once the agent picks a tool the bot sends a short, natural,
topic-aware «I'm on it» line (`dispatcher._progress_line` via the `on_progress` callback —
«Зазираю в кабінет інтернету…», «Підіймаю показники газу…»; varied, deterministic). This
progress line is sent as text **only for typed asks**. On a **voice** ask it's suppressed
(`_say_progress` no-ops when `voice_reply` — the «записує аудіо…» header already signals
work, so a stray text bubble before the voice reply is avoided), but `on_progress` is
**still wired** (not None) so the dispatcher composes the reply as **just the data** (no
«зараз гляну» preamble) — which is what then gets synthesized. A plain chat reply (no tool)
just answers — no progress line. The audio file is
deleted right after (transient; bytes never logged). Empty/failed transcript → «не розчув,
напиши текстом». **Meter values stay photo-only** — STT misreads digits, so a voice turn
can ask or act but never files a reading; destructive actions (delete) keep their confirm-tap.

**Voice in → voice out (L2.6):** a voice ask is **answered by voice**. `on_voice` calls
`_respond_to_text(…, voice_reply=True)`; the chat header shows **«записує аудіо…»**
(`_thinking(action="record_voice")`, not «друкує…») while the agent works and the reply is
synthesized locally (`agent/tts.py`: `TTSProvider` ABC + `PiperTTSProvider` default +
`Null…` when `TTS_PROVIDER=none`) and sent as a Telegram **voice note** (`answer_voice`);
any buttons (pay link / delete-confirm) ride on the voice message, and a chart/photo is
still attached as an image. Piper is an **external binary** (like `claude_bin` — no pip
dep): text → WAV, then ffmpeg → OGG/Opus (mono 48 kHz). The OGG is sent then deleted
(transient; bytes never logged). Replies are written for the screen, so `tts.voiceify`
normalizes them to natural spoken Ukrainian first: drops emoji/quotes/brackets/markup;
money → declined «510 гривень [10 копійок]» (with `_ua_plural` + de-grouped thousands;
.00 dropped); ISO dates → «шостого червня дві тисячі двадцять шостого року»; a **period**
«2026-06»/«червень 2026» → «червень дві тисячі двадцять шостого року» (`_MONTH_YEAR_RE` —
year in full + «року», not a bare cardinal that dead-ended «червень 2026»); **meter volumes**
«3.03 м³» → «3 кома нуль три кубометра» (`_VOLUME_RE`/`_volume_words`, declined unit — so a
reading isn't a unitless «сума»; `_meter_history_message` now emits `м³`); «20-го» →
«двадцятого»; decimals → «1888 кома 14» with **leading zeros voiced** («03» → «нуль 3», so
`3.03` isn't heard as `3.3` — `_spoken_frac`); a **dotted code** (login/contract «00.28.00.36»,
≥3 groups so a plain decimal is untouched) → pause-separated groups «00, 28, 00, 36»
(`_CODE_RE`) so it isn't read «00 крапка 28 крапка…»; a **bare ≥6-digit identifier** (the
Gigabit+ login/contract is a separator-less digit run like «00280036») → spoken
digit-by-digit «0 0 2 8 0 0 3 6» (`_DIGIT_ID_RE`, runs last), because espeak otherwise reads
a long run as a grouped cardinal and voices the thousands separators as «крапка» («00280036»
→ «00 крапка 280 крапка 036»); a lookahead skips a run already worded as money/percent/volume
(«100000 гривень» stays a number); **Latin brand/jargon** → a spoken Ukrainian
form (`_SPOKEN_TERMS`: «monobank» → «монобанк», else espeak says «монобайк»; «autopay»,
«Gigabit+»); dashes → pauses; lines folded into sentences.
(Numerals stay as digits — espeak-ng voices them.) **Stress — fixed via an espeak
pronunciation dictionary, NOT via the text.** espeak's `uk` rules mis-stress some words
(verified on the box: `подано → подА́но`, `баланс → бА́ланс`; both wrong). Nothing in the
text fed to Piper can correct it — tested through Piper's own phonemizer: a U+0301 accent
is **ignored**, a `[[phoneme]]` block is read **literally** (Piper says «відкрита квадратна
дужка»), and a Cyrillic respelling moves stress only by injecting an audible junk vowel.
The one override that reaches Piper is an espeak **pronunciation dictionary** (`uk_list` →
compiled `uk_dict`), which flows through `phonemize`. So `scripts/build_espeak_stress.py`
compiles `scripts/uk_stress_overrides.txt` (`подано p'odano`, `баланс bal'ans`,
`чіпав tSip'av`, …, `'` = primary stress; ч → espeak's affricate mnemonic `tS`) into a
**copy** of Piper's bundled `espeak-ng-data` (bundle untouched) and
Piper is pointed at it via `--espeak_data` (`tts._espeak_data_dir`: explicit
`PIPER_ESPEAK_DATA` wins, else the default `~/.dvoretskyi/espeak-ng-data` is auto-used iff
that build ran — no .env edit, no restart). Extensible: add a verified word to the list and
re-run the script. It's a **one-time VPS step** (the data dir lives outside the repo, like
the voice model). Correct stress for the *whole* lexicon would still need a non-espeak TTS
(`ukrainian-tts`/ESPnet, torch-heavy → a separate box, never the shared main VPS); the
dictionary covers the words we actually hit. (U+0301-as-stress in espeak is an unmerged
2025 proposal, issue #2241 — irrelevant here since Piper ignores it regardless.)
**Graceful fallback to text** on any miss, in two places: (1) `synthesize` returns None —
synth disabled, no voice model (`PIPER_VOICE` empty), reply over `TTS_MAX_CHARS`, or a
synth error; (2) the voice **send** is refused — `_try_voice` catches it and returns False
(most often `VOICE_MESSAGES_FORBIDDEN`, the recipient's Telegram privacy setting forbidding
voice notes from bots) → the bot sends text instead. So a voice asker is never left
empty-handed (and deploying before the voice model is installed is safe). `_try_voice` is
**stateless** — the moment the recipient allows voice notes the next turn delivers one, no
restart. Typed asks are unaffected (`voice_reply` defaults False).

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
pytest -q                       # 242 tests, in-memory SQLite, no network, no API key
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
`MONO_TOKEN`, `MONO_WEBHOOK_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID`
(the **owner** — payment/balance nudges + webhook payment confirmations go here alone),
`TELEGRAM_EXTRA_ALLOWED_USER_IDS` (optional CSV of extra user IDs — family — who may
**talk** to the bot; the owner is always allowed, these only widen the allowlist. They get
**no payment/balance nudges**, but the **meter «кинь фото» nudge is broadcast to them too**
since anyone admitted may submit a reading; `Settings.allowed_user_ids` = owner ∪ extras
feeds `AllowlistMiddleware` and the meter-nudge fan-out),
`DATABASE_URL`, `REDIS_URL`, `CLAUDE_BIN`, `CLAUDE_MODEL` (pins the decision turn's
model — default `claude-sonnet-4-6`; fast yet keeps the persona witty; empty → CLI
default), `LLM_PROVIDER` (claude_code|anthropic_api),
`UTILITY_MCCS`, `TZ`, `PUBLIC_BASE_URL`. **infolviv:** `INFOLV_LOGIN`, `INFOLV_PWD`,
`INFOLV_SUBMIT_ENABLED` (default false — live POST stays off until the body is verified).
**Meters:** `CLAUDE_VISION_TIMEOUT_SECONDS` (vision is slower than text),
`SUBMISSION_CHANNELS=gas:manual,water:manual`, `SMS_GATEWAY_URL` (empty → deep link only),
`OCR_MAX_LONG_SIDE`, `DELTA_SPIKE_K`, `DELTA_ABS_CAP`, `METER_WINDOW_DAYS`,
`METER_SUBMIT_FROM_DAY` (28), `METER_EARLY_SUBMIT_ATTEMPTS` (3),
`METER_PHOTO_DIR` (empty → `~/.dvoretskyi/meter_photos`), `METER_PHOTO_MAX_LONG_SIDE`
(1000), `METER_PHOTO_QUALITY` (55).
**Payment reminders:** `PAYMENT_NUDGE_WINDOW_DAYS` (5 — days before `due_day` to start
nudging, with a type-routed pay link).
**Voice in:** `STT_PROVIDER` (whisper|none), `WHISPER_MODEL` (small default; base to save
RAM), `WHISPER_COMPUTE_TYPE` (int8), `WHISPER_LANGUAGE` (uk), `STT_TIMEOUT_SECONDS`.
**Voice out:** `TTS_PROVIDER` (piper|none), `PIPER_BIN` (piper executable), `PIPER_VOICE`
(path to the .onnx voice model; **empty → no synth, text reply** — so deploy is safe
before it's installed), `PIPER_LENGTH_SCALE` (speaking rate, <1 faster / >1 slower,
default 0.9), `PIPER_SENTENCE_SILENCE` (pause after each sentence, default 0.3s),
`PIPER_ESPEAK_DATA` (custom espeak data dir for the uk stress overrides; empty →
auto-use `~/.dvoretskyi/espeak-ng-data` if `scripts/build_espeak_stress.py` was run there,
else Piper's bundled data), `TTS_TIMEOUT_SECONDS` (30), `TTS_MAX_CHARS` (600 — longer
replies go out as text).

## Households (two properties, Phase A+)
Two properties: **primary** (home; all 7 providers, photo meters) and **secondary**
(unoccupied; pays only Електроенергія (ЛЕЗ) + Газ (доставлення), no payment nudges yet;
its gas meter is a **static** value filed monthly). `Household` (`db/models.py`) owns its
providers via `Provider.household_id`; `Provider.name` is unique **per household**
(`uq_provider_household_name`), so shared utilities exist once per property. **Addresses
are PII → never in code**: code uses slugs (`households.PRIMARY`/`SECONDARY` in
`households.py`); display names + infolviv account codes + the static gas value come from
env (`HOUSEHOLD_*`, VPS only), seeded into the DB by `dvoretskyi seed-providers` (now
seeds households first, then per-household providers; `SECONDARY_PROVIDERS` in `cli.py`).
`households.resolve(text)` maps a slug or address fragment → `Household`;
`_provider_by_name(name, household?)` disambiguates a shared name (defaults to primary).
**Stats are combined or split:** `get_stats(…, household?, breakdown="household",
provider?)` — no `household` = combined across both (default, unchanged);
`household=<slug/address frag>` filters to one property (title names it);
`breakdown="household"` splits the total by property. The LLM passes the user's wording as
`household`; `resolve` matches it to the env name. **`provider` narrows to one service:**
a name or category keyword (case-insensitive substring), so «скільки за газ» → `provider=
"газ"` catches **both** gas providers (постачання + доставлення), «вода»/«інтернет»
likewise; it **intersects** with `household` (allowed pids = household pids ∩ matched
pids), so «сума за газ на Зеленій 151» answers only that property's gas — not the
whole-household total. The title becomes «<провайдер> · <житло> · <період>». **Payment routing:** the categorize prompt's buttons carry the
household-specific `provider_id`; `categorize_keyboard` suffixes « · <житло>» on names
shared across properties (ЛЕЗ, Газ доставлення); the tap threads the exact household into
`categorize_payment(…, household=…)`. **Every confirmation names the household**
(`_household_suffix` in `bot/app.py` — «✅ <провайдер> · <житло> — … записав») so it's
always clear where it landed; «запам'ятав» is shown only when a pattern was actually
learned. **Мобільний is exempt** from the suffix (`Category.mobile`): a phone top-up isn't
tied to a property, so «✅ Мобільний — 600 ₴…» drops the « · <житло>». An **unknown** tx
prompt names the payee, not the bare number: `_payee_hint` collapses monobank's
newline-joined fields and strips ≥6-digit runs (phone / особовий рахунок), so a Lifecell
top-up («Lifecell\n+380…») reads «Прилетіло 600 ₴ від «Lifecell», а такого в мене нема. Це
що?» (the matcher still learns the `stable_token` «lifecell», never the phone). Secondary (Шашкевича) seeds **ЛЕЗ + Газ (постачання) + Газ (доставлення)**
(`SECONDARY_PROVIDERS`); the static gas meter sits on its Газ (доставлення). **Shared-utility routing — home is the default**
(`mono/matcher.py`, because monobank's webhook carries **nothing** that distinguishes the
two properties: only `description`=«Електроенергія», `mcc`, a per-tx `receiptId` — no
`comment`/counterparty/IBAN, verified live). A **shared-name** provider
(`_ambiguous_provider_ids`): the **primary/home** one learns its **letter** token →
bare «Електроенергія» auto-routes home; the **secondary** one is reachable only by its
**особовий рахунок** (`account_token`, longest ≥6-digit run) — and a digit pattern beats a
letter token in `match` (`_more_specific`). Since real flat payments carry no account
number, they too land on home and the user re-points them with **one tap**: the LOGGED
confirmation carries «↪ Це <інше житло>» (`ch:<payment_id>` → `on_correct_household` →
`_other_household_provider` re-points to the same-named provider in the other household).
A bare **category keyword** (`UTILITY_KEYWORDS`) is never learned (it'd hijack siblings —
«газ» ⊂ «Газ (доставлення)»); a digit already routing elsewhere (shared EDRPOU) is dropped.
**Photos are primary-only:** `_meter_providers` filters to the primary
household (the secondary meter is static, filed without a photo).
**Secondary static meter (Phase D):** a provider with `static_reading` set is unoccupied
→ its month-end meter nudge stages a `validated` `MeterReading` with that fixed value and
sends the «📤 Подати на портал» approve tap (the `Notifier`/`_send` gained
`approve_reading_id`; engine stays aiogram-free) instead of «кинь фото». Filing routes by
household: `infolviv.reading_for_kind(kind, account_code=…)` and
`submit_infolviv_reading(kind, value, account_code=…)` take the household's
`infolviv_account_code` (the two **gas** counters share one login → the account
disambiguates them); `_file_reading` looks it up from the reading's provider→household.
A **unique** kind (water — one counter, its own account) isn't gated: if the account
matches no counter of that kind, `reading_for_kind` falls back to the lone counter. So
`HOUSEHOLD_*_INFOLVIV_ACCOUNT` is each property's **gas** account; water self-resolves.
`None` account = first matching counter (single-household back-compat). Kill-switch
unchanged.
Migration `0004` adds it all (batch + `naming_convention` to drop the old `name` unique on
SQLite). Tests/fixtures use fake names («Житло 1/2»), never real addresses.

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
- `get_provider_balance` is **live** for Інтернет (Gigabit+) and Мобільний: `agent/balance.py`
  scrapes the Gigabit+ cabinet (CSRF login → user-state JSON) for balance, **last top-up**,
  the **monthly fee** (`tarif_plan.month_fee` — not hardcoded), and the reply also carries
  the **login/contract number** (`gigabit_login` from env, the owner's own data) so «нагадай
  мій логін / яка абонплата» are answered from the tool, never from the model's memory; login
  survives even when the cabinet is down. Other providers still raise `NotImplementedError`.
- Still stubbed: `WebFormChannel` live submit (provider auth not reverse-engineered);
  `AnthropicAPIProvider` is a drop-in swap, not yet implemented.
