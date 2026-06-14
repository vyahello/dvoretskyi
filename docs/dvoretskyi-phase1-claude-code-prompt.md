# Claude Code — Phase 1 Implementation Prompt: Комунальний Дворецький

> Paste this into a Claude Code session at the repo root. The architecture spec is
> at `docs/dvoretskyi-agent-spec.md` — **read it first**; this prompt is the build
> order and the hard contracts for Phase 1 only.

---

## Role & scope

Build **Phase 1** of a single-user Telegram utility agent ("Комунальний Дворецький"). Phase 1 is
the **money + stats + reminders + conversational agent** layer, sourced entirely
from the **monobank webhook**. No provider scraping, no meter OCR, no payment
initiation.

### In scope (Phase 1)
- mono personal-API webhook receiver → log & categorize payments (idempotent)
- categorize-and-learn for unmatched **utility-candidate** transactions
- provider registry (6 providers, seeded)
- agent: free-text → LLM decision → tool execution → in-character reply
- `LLMProvider` abstraction with a working `ClaudeCodeProvider` (headless `claude -p`)
- bot tools over internal data: unpaid status, stats, manual log, categorize, snooze
- payment reminders (APScheduler)
- pytest suite, in-memory SQLite for tests

### Explicitly OUT of scope (do NOT build — later phases)
- Any web scraping or HTTP calls to providers (ЛЕЗ/Колумбус/gas.ua/ВК)
- Meter-reading OCR or submission (`submit_meter_reading`, `get_provider_balance`
  → define signatures only, `raise NotImplementedError`)
- Payment initiation / mono deep links / autopay
- Meter-window reminders (scaffold the `kind="meter"` branch but leave inactive)
- Multi-user support, web dashboard

If you find yourself reaching outside this list, stop and ask.

---

## Stack (pinned — do not substitute)

- Python 3.12, `uv` or `pip` (match existing repo convention)
- FastAPI (webhook + app lifespan), aiogram 3 (Telegram)
- SQLAlchemy 2.0 (typed `Mapped[...]` declarative), Alembic (migrations)
- pydantic-settings (config), Redis (APScheduler jobstore + dedupe locks)
- APScheduler (reminders)
- pytest, pytest-asyncio
- LLM: headless Claude Code CLI via subprocess (see `ClaudeCodeProvider` below)

---

## Project structure

```
src/dvoretskyi/
  config.py              # pydantic-settings Settings
  app.py                 # FastAPI app; lifespan starts bot polling + scheduler
  cli.py                 # admin CLI: seed-providers, register-mono-webhook (--dry-run)
  db/
    session.py           # engine, async session factory
    models.py            # SQLAlchemy 2.0 models
  mono/
    schemas.py           # pydantic models for StatementItem payload
    webhook.py           # FastAPI router: POST /mono/webhook/{secret}
    matcher.py           # description→provider; is_utility_candidate(); learn_pattern()
    client.py            # register webhook via mono API (used by CLI)
  agent/
    persona.py           # BUTLER_SYSTEM_PROMPT constant (Ukrainian, from spec §5a)
    provider.py          # LLMProvider ABC + ClaudeCodeProvider + Decision
    tools.py             # tool functions + TOOLS registry
    dispatcher.py        # handle_message(text, ctx) -> reply
  bot/
    app.py               # aiogram Bot/Dispatcher, handlers, allowlist middleware
    keyboards.py         # inline keyboards (categorize, snooze, confirm)
  reminders/
    engine.py            # APScheduler setup + daily nudge job
tests/
  conftest.py            # in-memory sqlite, fixtures, fake LLMProvider, fake Bot
  test_matcher.py  test_webhook.py  test_tools.py  test_dispatcher.py  test_reminders.py
CLAUDE.md                # operational notes, ≤120 lines
.env.example
```

---

## Data models (`db/models.py`)

```
Provider
  id: int PK
  name: str (unique)                 # "Газ", "Газ (доставлення)", "Вода", ...
  category: Enum(water,electricity,gas,internet,housing,mobile)
  account_number: str | None         # особовий рахунок (shared across the 5)
  pay_channel: Enum(mono_communal, mono_card, off_mono)
  expected_amount: Decimal | None    # for "unpaid?" hint; null = variable (ДАХ)
  due_day: int | None                # day-of-month payment deadline
  auto_logged: bool                  # appears in mono «Комуналка»

ProviderPattern
  id: int PK
  provider_id: FK
  pattern: str                       # case-insensitive substring matched on tx description
  source: Enum(seed, learned)

Payment
  id: int PK
  provider_id: FK | None             # null = uncategorized
  amount_uah: Decimal
  paid_at: datetime (tz-aware, Europe/Kyiv)
  source: Enum(mono_webhook, manual)
  raw_description: str
  mcc: int | None
  mono_tx_id: str | None UNIQUE      # idempotency key; null for manual

NudgeLog
  id: int PK
  provider_id: FK
  cycle: str                         # "YYYY-MM"
  kind: Enum(payment, meter)         # only `payment` active in Phase 1
  nudged_at: datetime
  snoozed_until: datetime | None
```

Provide an Alembic initial migration. Decimal money (never float). All datetimes tz-aware, `Europe/Kyiv`.

### Seed data (`cli.py seed-providers`)
Seed the 6 providers from spec §3a: Вода, Електроенергія (ЛЕЗ), Газ, Газ
(доставлення) — `auto_logged=True`, `pay_channel=mono_communal`; Інтернет
(Колумбус) and Кварплата (ДАХ) — `auto_logged=False`, `pay_channel=mono_card`,
ДАХ `expected_amount=None`. Leave `ProviderPattern.pattern` values as TODO
placeholders — real mono `description` strings get captured from live transactions
(spec open question #2) and added via `cli.py learn-pattern` or the bot.

---

## mono webhook (`mono/webhook.py` + `matcher.py`)

1. `POST /mono/webhook/{secret}` — reject if `secret != settings.mono_webhook_secret`.
   mono also sends GET to validate the URL on registration → return 200.
2. Payload: `{"type":"StatementItem","data":{"account":..,"statementItem":{id,time,description,mcc,amount,...}}}`.
   `amount` is in kopiykas, **negative for outflow**. Parse with pydantic (`mono/schemas.py`).
3. **Idempotency:** if `mono_tx_id` already in `Payment`, return 200 and stop.
4. **Outflows only:** ignore `amount >= 0` (top-ups/refunds) in Phase 1.
5. **Match:** `matcher.match(description)` → Provider | None (case-insensitive substring
   over `ProviderPattern`).
   - Match → create `Payment(source=mono_webhook, provider=...)`; push the butler's
     confirmation to Telegram.
   - **No match:** only act if `matcher.is_utility_candidate(mcc, description)` is true
     — i.e. `mcc in settings.utility_mccs` (default `{4900, 4814}`: utilities, telecom)
     **or** description hits a keyword list (газ, вода, енерг, осбб, домоуправ,
     інтернет, провайдер…). Otherwise **ignore** (it's a coffee, not комуналка).
     If candidate → store an **uncategorized** `Payment(provider_id=None)` and send a
     categorize prompt (inline buttons: the 6 providers + «Не комуналка»).
6. **Learn:** on categorize callback, set `Payment.provider_id` and insert a
   `ProviderPattern(source=learned)` from a stable token of `raw_description` so the
   next identical payee auto-logs. «Не комуналка» → delete the uncategorized payment.

Webhook handler must be fast: validate + enqueue/commit, push Telegram message
async. The `Bot` instance is shared via FastAPI app state.

---

## Agent (`agent/`)

### `Decision` + `LLMProvider` (`provider.py`)
```python
@dataclass
class Decision:
    tool: str | None
    args: dict
    message: str          # Ukrainian, in the butler's voice

class LLMProvider(ABC):
    @abstractmethod
    async def decide(self, user_text: str, context: dict) -> Decision: ...
```

### `ClaudeCodeProvider`
- Builds the full prompt: `BUTLER_SYSTEM_PROMPT` (persona) + serialized `context`
  (open providers, current cycle, recent payments) + the user's text + a strict
  instruction to **return only** `{"tool","args","message"}` JSON.
- Invokes headless Claude Code via `asyncio.create_subprocess_exec`:
  `claude -p <prompt> --output-format json --allowedTools ""`
  (inject persona via `--append-system-prompt` if preferred — verify the current
  flag name with `claude --help`; tools disabled so it acts as a pure LLM endpoint).
- **Env hardening:** spawn with an explicit env where `ANTHROPIC_API_KEY` is removed
  (`env = {k:v for k,v in os.environ.items() if k != "ANTHROPIC_API_KEY"}`). This is
  mandatory — see spec §8 (silent API-billing footgun).
- Parse: outer JSON has the model text in `.result`; parse that inner string into
  `{tool,args,message}`. Be defensive (strip ``` fences, retry once on parse fail,
  fall back to `Decision(tool=None, args={}, message=<safe Ukrainian apology line>)`).
- Apply a timeout (e.g. 60s) and surface failures gracefully to the user.

### `AnthropicAPIProvider`
- Interface-complete stub (`raise NotImplementedError` in `decide`) so it's a
  drop-in swap later. Selection via `settings.llm_provider`.

### Tools (`tools.py`)
Pure functions over the DB; return plain dicts. `TOOLS` registry maps name → callable.
- `get_unpaid(cycle=None) -> {open: [{provider, expected_amount, due_day, days_left}], all_clear: bool}`
- `get_stats(period, breakdown="provider"|"month") -> {total, items:[...], chart_path?}`
  (chart = matplotlib PNG to a temp path; bot sends as photo)
- `log_payment_manual(provider_name, amount) -> {...}`
- `categorize_payment(mono_tx_id, provider_name) -> {...}`  (also learns pattern)
- `snooze_reminder(provider_name, until) -> {...}`
- Phase-2 stubs (signatures only, `NotImplementedError`): `submit_meter_reading`,
  `get_provider_balance`

### `dispatcher.py`
`handle_message(user_text, ctx) -> reply`: build context (call `get_unpaid` + recent
payments) → `provider.decide(...)` → if `decision.tool`, execute via `TOOLS` and let
the butler phrase the result (either a second LLM turn or template the tool output into
`decision.message`) → return final reply + any media. The **tool routing is
deterministic**; the persona governs only the `message` text.

---

## Bot (`bot/app.py`)
- aiogram 3. **Allowlist middleware:** drop any update whose `from_user.id !=
  settings.telegram_allowed_user_id` (no reply).
- Text handler → `dispatcher.handle_message`.
- Callback handlers (inline buttons) call tools **directly** (categorize, snooze,
  confirm) — don't route button taps through the LLM.
- Started in FastAPI lifespan (long polling is fine for single-user; no public bot
  endpoint needed).

---

## Reminders (`reminders/engine.py`)
- APScheduler (AsyncIO scheduler, Redis jobstore), `Europe/Kyiv`.
- Daily job: for each provider with `due_day`, current cycle, `kind="payment"`:
  nudge **iff** unpaid this cycle AND within the nudge window (e.g. due_day−3 … due_day)
  AND not snoozed AND not already nudged today. Near deadline (due_day or due_day−1)
  → escalation copy. Send via Bot in the butler's voice. Record `NudgeLog`.
- Scaffold a `kind="meter"` branch but keep it disabled in Phase 1.

---

## Config (`config.py`, pydantic-settings) & `.env.example`
`MONO_TOKEN`, `MONO_WEBHOOK_SECRET`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_ALLOWED_USER_ID`, `DATABASE_URL`, `REDIS_URL`, `CLAUDE_BIN=claude`,
`LLM_PROVIDER=claude_code`, `UTILITY_MCCS=4900,4814`, `TZ=Europe/Kyiv`.
Document in `.env.example`. **Do not** define `ANTHROPIC_API_KEY`.

---

## Security (enforce in code, per spec §8)
- Spawn `claude` with `ANTHROPIC_API_KEY` stripped from env (mandatory).
- Webhook secret in the path + reject mismatches; idempotency by `mono_tx_id`.
- Telegram allowlist; no card data anywhere; Decimal money only.

---

## Tests (pytest, in-memory SQLite, target ≥80% on core logic)
- `test_matcher`: substring matching (case-insensitive), `is_utility_candidate`
  (4900/4814 vs 5814 grocery → ignored), learn → next match auto.
- `test_webhook`: idempotency (dup tx ignored), outflow-only, known-pattern auto-log,
  utility-candidate-unmatched → uncategorized + prompt, non-candidate → ignored.
- `test_tools`: `get_unpaid` cycle boundary logic; stats aggregation by
  provider/month; manual log; categorize learns a pattern.
- `test_dispatcher`: inject a **fake `LLMProvider`** returning canned `Decision`s;
  assert correct tool routing and that persona text is passed through untouched.
- `test_reminders`: nudge fires for overdue, suppressed when paid/snoozed/already
  nudged; near-deadline escalation. Freeze time.

---

## Working conventions
- Build in the order: config → models + migration → matcher → webhook → tools →
  provider → dispatcher → bot → reminders → tests. Keep each step runnable/testable.
- Conventional commits, one logical change per commit.
- Write `CLAUDE.md` (≤120 operational lines): run/test/deploy commands, env vars,
  the no-`ANTHROPIC_API_KEY` rule, "Phase 1 scope only".
- `cli.py register-mono-webhook` must support `--dry-run` (print the request, don't send).
- Ask before adding any dependency not listed above, or before touching anything in the OUT-of-scope list.

## Definition of done
1. A real mono outflow matching a seeded pattern is logged once (idempotent) and
   the butler confirms it in Telegram, in character.
2. A utility-candidate unmatched tx produces a categorize prompt; tapping a provider
   logs it and the next identical payee auto-logs.
3. A non-utility purchase (e.g. grocery, MCC 5814) is silently ignored.
4. Free-text "що мені треба заплатити?" returns the correct open providers for the cycle.
5. Manual log + snooze work via tools.
6. A daily run nudges an overdue provider once, in character, and respects snooze.
7. `pytest` green; the service boots and runs with **no `ANTHROPIC_API_KEY`** present.
