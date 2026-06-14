# Deploy — Комунальний Дворецький

Deployment architecture and runbook. **All host-specific values below are
placeholders** — substitute your own and keep real values out of the repo:

| Placeholder | Meaning |
|---|---|
| `<deploy-user>` | the Linux user the service runs as |
| `<vps-host>` | VPS hostname or IP |
| `<your-domain>` | public domain (DuckDNS → VPS IP) |
| `<owner>` | GitHub owner/org of the repo |
| `<redis-port>` / `<db-index>` | port + logical DB of the shared Redis instance |

App path throughout: `/home/<deploy-user>/dvoretskyi`.

## Architecture (deployment overview)

| Aspect | Config |
|---|---|
| Host | VPS `<vps-host>`, Ubuntu 24.04, Python 3.12 |
| App path | `/home/<deploy-user>/dvoretskyi` (venv at `venv/`, SQLite `dvoretskyi.db`) |
| Domain | `<your-domain>` (DuckDNS → VPS IP) |
| TLS | Let's Encrypt via certbot, with automatic renewal (`certbot.timer`) |
| nginx | server block from `deploy/nginx-dvoretskyi.conf`, proxies → `127.0.0.1:8100` |
| systemd | `dvoretskyi.service`: `User=<deploy-user>`, `EnvironmentFile=…/.env`, `ExecStart=…/venv/bin/uvicorn dvoretskyi.app:app --host 127.0.0.1 --port 8100`, `Environment="ANTHROPIC_API_KEY="` (empty), `Restart=always`, `RestartSec=5` |
| Scheduler | APScheduler on a shared Redis instance, isolated logical DB: `redis://127.0.0.1:<redis-port>/<db-index>` (`REDIS_URL`) |
| LLM | Claude Code headless on the **Max** plan via `CLAUDE_CODE_OAUTH_TOKEN` in `.env` |
| sudoers | `<deploy-user>` NOPASSWD limited to `systemctl restart dvoretskyi` |

## CI/CD flow
Push to `main` → CI (`ruff` + `mypy` + `pytest`) → on green, the `deploy` job SSHes in
and runs `scripts/deploy.sh` on the VPS:
`git reset --hard origin/main` → `pip install -e ".[dev]"` → **backup** `dvoretskyi.db`
→ `alembic upgrade head` → `dvoretskyi seed-providers` → `sudo systemctl restart dvoretskyi`
→ `systemctl is-active` health check.

## mono webhook
Public endpoint: `https://<your-domain>/mono/webhook/<secret>` (the `<secret>` is
`MONO_WEBHOOK_SECRET`). Register once (reads token + secret from `.env`):
```bash
cd /home/<deploy-user>/dvoretskyi && source venv/bin/activate
dvoretskyi register-mono-webhook --dry-run   # inspect the request (token masked)
dvoretskyi register-mono-webhook             # register with mono
```

## Required secrets (names only — never commit values)
**GitHub** (Settings → Secrets and variables → Actions) — used by the deploy job:
`VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `VPS_APP_DIR` (the app path on the VPS, e.g.
`/home/<deploy-user>/dvoretskyi`).

**VPS `/home/<deploy-user>/dvoretskyi/.env`** (`chmod 600`; CI never reads it):
`MONO_TOKEN`, `MONO_WEBHOOK_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID`,
`CLAUDE_CODE_OAUTH_TOKEN`, `REDIS_URL`, `DATABASE_URL`.

## One-time VPS setup (Ubuntu 24.04)

> CI self-heals a missing clone and venv (the deploy job clones the repo and
> `deploy.sh` creates the venv on first run) — **but only if `<deploy-user>`'s SSH key is
> authorized on GitHub** for the clone. Regardless, you must still do these once before
> the first deploy can go green: create **`.env`** (secrets) and install the **systemd
> unit + sudoers rule** (the deploy's `systemctl restart` needs them).

```bash
# 1. Clone
cd /home/<deploy-user>
git clone git@github.com:<owner>/dvoretskyi.git
cd dvoretskyi

# 2. venv + install (Python 3.12)
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

# 3. Create .env BY HAND (never committed). Fill every secret:
#    MONO_TOKEN=...            TELEGRAM_BOT_TOKEN=...        TELEGRAM_ALLOWED_USER_ID=...
#    MONO_WEBHOOK_SECRET=...   CLAUDE_CODE_OAUTH_TOKEN=...
#    DATABASE_URL=sqlite+aiosqlite:///./dvoretskyi.db
#    REDIS_URL=redis://127.0.0.1:<redis-port>/<db-index>
#    PUBLIC_BASE_URL=https://<your-domain>
#    TZ=Europe/Kyiv
cp .env.example .env && nano .env
chmod 600 .env

# 4. Auth Claude Code under the Max subscription (writes the OAuth token):
claude setup-token
#    put the resulting token into .env as CLAUDE_CODE_OAUTH_TOKEN=...

# 5. First migration + seed
alembic upgrade head
dvoretskyi seed-providers
```

### 6. systemd unit (from the template — substitute `<deploy-user>`)
```bash
sudo cp deploy/dvoretskyi.service.template /etc/systemd/system/dvoretskyi.service
sudo sed -i "s/<deploy-user>/$USER/g" /etc/systemd/system/dvoretskyi.service
sudo systemctl daemon-reload
sudo systemctl enable --now dvoretskyi
systemctl status dvoretskyi
```
The unit pins `Environment="ANTHROPIC_API_KEY="` (empty) so Claude Code uses the Max
subscription (`CLAUDE_CODE_OAUTH_TOKEN`) and never falls back to paid API billing.

### 7. Passwordless restart for the deploy script
`scripts/deploy.sh` runs `sudo systemctl restart dvoretskyi` — allow just that:
```bash
echo '<deploy-user> ALL=(root) NOPASSWD: /usr/bin/systemctl restart dvoretskyi' \
  | sudo tee /etc/sudoers.d/dvoretskyi
sudo chmod 440 /etc/sudoers.d/dvoretskyi
sudo visudo -c        # validate
```

### 8. nginx + TLS
```bash
sudo cp deploy/nginx-dvoretskyi.conf /etc/nginx/sites-available/dvoretskyi
sudo ln -s /etc/nginx/sites-available/dvoretskyi /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d <your-domain>      # provisions + wires TLS, enables auto-renew
```

### 9. Register the mono webhook (once, after HTTPS is live)
```bash
source venv/bin/activate
dvoretskyi register-mono-webhook --dry-run   # inspect (token masked)
dvoretskyi register-mono-webhook             # registers https://<your-domain>/mono/webhook/<secret>
```

## Routine deploys
Push to `main` → CI runs tests → on green, deploys automatically. Manual:
`ssh <deploy-user>@<vps-host> 'bash /home/<deploy-user>/dvoretskyi/scripts/deploy.sh'`.
Each deploy backs up `dvoretskyi.db` to `dvoretskyi.db.bak.<epoch>` before migrating.

## Troubleshooting

First look — status, live logs, is it listening:
```bash
systemctl status dvoretskyi --no-pager
journalctl -u dvoretskyi -n 50 --no-pager      # recent
journalctl -u dvoretskyi -f                     # follow live (e.g. while messaging the bot)
ss -ltnp | grep 8100                            # should show uvicorn on 127.0.0.1:8100
systemctl show -p MainPID -p ActiveEnterTimestamp dvoretskyi   # current pid + since when
```

**`.env` edits do nothing until restart** — systemd reads `EnvironmentFile` only at
start. After editing `.env`: `sudo systemctl restart dvoretskyi`. Check what the app
actually resolved (run from the WorkingDirectory so it reads the right `.env`):
```bash
cd /home/<deploy-user>/dvoretskyi
venv/bin/python -c "from dvoretskyi.config import get_settings as g; s=g(); \
  print('redis', s.redis_url); print('db', s.database_url); print('tz', s.tz)"
```

**Common failure signatures (`journalctl`):**
| Symptom | Cause → fix |
|---|---|
| `Unit dvoretskyi.service not found` | unit not installed → §6 |
| `status=203/EXEC` | bad `ExecStart` path → confirm `venv/bin/uvicorn` exists (`pip install -e .`) |
| `Failed to ... EnvironmentFile` | `.env` missing at `/home/<deploy-user>/dvoretskyi/.env` |
| `Address already in use` (8100) | something else bound → `ss -ltnp \| grep 8100` |
| `reminders: Redis unavailable ... using in-memory jobstore` | `REDIS_URL` wrong/unreachable → see Redis below (non-fatal; nudges still run in-process) |
| `Can't pickle local object` on startup | regression of the jobstore closure bug — scheduled jobs must be module-level with no closure args (`reminders/engine.py`) |
| bot silent in Telegram | check logs for `TelegramNetworkError` / token; verify `TELEGRAM_BOT_TOKEN` + `TELEGRAM_ALLOWED_USER_ID` in `.env` (only the allowlisted user gets replies) |

**Redis** (scheduler uses the configured logical DB on the shared instance):
```bash
grep ^REDIS_URL /home/<deploy-user>/dvoretskyi/.env        # redis://127.0.0.1:<redis-port>/<db-index>
python3 -c "import socket;s=socket.socket();s.settimeout(2);\
  print('OPEN' if s.connect_ex(('127.0.0.1',<redis-port>))==0 else 'CLOSED')"
redis-cli -h 127.0.0.1 -p <redis-port> -n <db-index> keys 'dvoretskyi.*'   # scheduled jobs
```

**Claude Code auth** (must use the Max subscription via `CLAUDE_CODE_OAUTH_TOKEN`, with
`ANTHROPIC_API_KEY` empty/unset):
```bash
cd /home/<deploy-user>/dvoretskyi && set -a && . .env && set +a
claude -p "ping" --output-format json | head -c 200; echo   # JSON result = auth OK
```

**Database / migrations:**
```bash
cd /home/<deploy-user>/dvoretskyi && source venv/bin/activate
alembic current                      # should be the latest revision (head)
alembic history --verbose | head
ls -la dvoretskyi.db*                 # .bak.<epoch> snapshots from each deploy
# restore a backup if a migration went wrong:
#   sudo systemctl stop dvoretskyi && cp dvoretskyi.db.bak.<epoch> dvoretskyi.db && sudo systemctl start dvoretskyi
```

**Webhook (mono) reachability** — needs nginx + TLS up:
```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://<your-domain>/health
sudo nginx -t && sudo systemctl reload nginx
sudo journalctl -u nginx -n 30 --no-pager
```

After any fix: `sudo systemctl restart dvoretskyi && systemctl is-active dvoretskyi`.
