# Deploy — Комунальний Дворецький

CI/CD: `.github/workflows/ci.yml` runs `ruff` + `mypy` + `pytest` on every push/PR to
`main`; a green `test` job then triggers `deploy`, which SSHes into the VPS and runs
`scripts/deploy.sh`. **Bot secrets live only in `/home/cax/dvoretskyi/.env` on the VPS —
CI never sees them.**

## GitHub repo secrets (Settings → Secrets and variables → Actions)
| Secret | Value |
|---|---|
| `VPS_HOST` | VPS IP / hostname |
| `VPS_USER` | `cax` |
| `VPS_SSH_KEY` | private SSH key whose **public** half is in `cax`'s `~/.ssh/authorized_keys` |

(No `MONO_TOKEN` / `TELEGRAM_BOT_TOKEN` / `CLAUDE_CODE_OAUTH_TOKEN` / `MONO_WEBHOOK_SECRET`
in GitHub — those are VPS-only.)

## One-time VPS setup (Ubuntu 24.04, user `cax`)

> CI self-heals a missing clone and venv (the deploy job clones into
> `/home/cax/dvoretskyi` and `deploy.sh` creates the venv on first run) — **but only if
> `cax`'s SSH key is authorized on GitHub** for the clone. Regardless, you must still do
> these once before the first deploy can go green: create **`.env`** (secrets), install
> the **systemd unit + sudoers rule** (the deploy's `systemctl restart` needs them). The
> steps below cover the full manual path; doing them all up front is the simplest.

```bash
# 1. Clone
cd /home/cax
git clone git@github.com:vyahello/dvoretskyi.git
cd dvoretskyi

# 2. venv + install (Python 3.12.3)
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

# 3. Create .env BY HAND (never committed). Fill every secret:
#    MONO_TOKEN=...            TELEGRAM_BOT_TOKEN=...        TELEGRAM_ALLOWED_USER_ID=...
#    MONO_WEBHOOK_SECRET=...   CLAUDE_CODE_OAUTH_TOKEN=...
#    DATABASE_URL=sqlite+aiosqlite:///./dvoretskyi.db
#    REDIS_URL=redis://127.0.0.1:6800/3      # Redis in Docker, logical DB 3
#    PUBLIC_BASE_URL=https://dvoretskyi.duckdns.org
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

### 6. systemd unit
```bash
sudo cp deploy/dvoretskyi.service /etc/systemd/system/dvoretskyi.service
sudo systemctl daemon-reload
sudo systemctl enable --now dvoretskyi
systemctl status dvoretskyi
```

### 7. Passwordless restart for the deploy script
`scripts/deploy.sh` runs `sudo systemctl restart dvoretskyi` as `cax` — allow just that:
```bash
echo 'cax ALL=(root) NOPASSWD: /usr/bin/systemctl restart dvoretskyi' \
  | sudo tee /etc/sudoers.d/dvoretskyi
sudo chmod 440 /etc/sudoers.d/dvoretskyi
sudo visudo -c        # validate
```

### 8. nginx + TLS
```bash
sudo cp deploy/nginx-dvoretskyi.conf /etc/nginx/sites-available/dvoretskyi
sudo ln -s /etc/nginx/sites-available/dvoretskyi /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d dvoretskyi.duckdns.org      # provisions + wires TLS
```

### 9. Register the mono webhook (once, after HTTPS is live)
```bash
source venv/bin/activate
dvoretskyi register-mono-webhook --dry-run   # inspect (token masked)
dvoretskyi register-mono-webhook             # registers https://dvoretskyi.duckdns.org/mono/webhook/<MONO_WEBHOOK_SECRET>
```

## Routine deploys
Push to `main` → CI runs tests → on green, deploys automatically. Manual:
`ssh cax@<host> 'bash /home/cax/dvoretskyi/scripts/deploy.sh'`. Each deploy backs up
`dvoretskyi.db` to `dvoretskyi.db.bak.<epoch>` before migrating.

## Troubleshooting (on the VPS, as `cax`)

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
cd /home/cax/dvoretskyi
venv/bin/python -c "from dvoretskyi.config import get_settings as g; s=g(); \
  print('redis', s.redis_url); print('db', s.database_url); print('tz', s.tz)"
```

**Common failure signatures (`journalctl`):**
| Symptom | Cause → fix |
|---|---|
| `Unit dvoretskyi.service not found` | unit not installed → §6 |
| `status=203/EXEC` | bad `ExecStart` path → confirm `venv/bin/uvicorn` exists (`pip install -e .`) |
| `Failed to ... EnvironmentFile` | `.env` missing at `/home/cax/dvoretskyi/.env` |
| `Address already in use` (8100) | something else bound → `ss -ltnp \| grep 8100` |
| `reminders: Redis unavailable ... using in-memory jobstore` | `REDIS_URL` wrong/unreachable → see Redis below (non-fatal; nudges still run in-process) |
| `Can't pickle local object` on startup | regression of the jobstore closure bug — scheduled jobs must be module-level with no closure args (`reminders/engine.py`) |
| bot silent in Telegram | check logs for `TelegramNetworkError` / token; verify `TELEGRAM_BOT_TOKEN` + `TELEGRAM_ALLOWED_USER_ID` in `.env` (only the allowlisted user gets replies) |

**Redis** (bot uses logical DB 3 on the Docker Redis at `127.0.0.1:6800`):
```bash
grep ^REDIS_URL /home/cax/dvoretskyi/.env          # expect redis://127.0.0.1:6800/3
python3 -c "import socket;s=socket.socket();s.settimeout(2);\
  print('6800', 'OPEN' if s.connect_ex(('127.0.0.1',6800))==0 else 'CLOSED')"
redis-cli -h 127.0.0.1 -p 6800 -n 3 keys 'dvoretskyi.*'   # scheduled jobs live here
```

**Claude Code auth** (must use the Max subscription via `CLAUDE_CODE_OAUTH_TOKEN`, no
`ANTHROPIC_API_KEY` in the env):
```bash
cd /home/cax/dvoretskyi && set -a && . .env && set +a
claude -p "ping" --output-format json | head -c 200; echo   # JSON result = auth OK
```

**Database / migrations:**
```bash
cd /home/cax/dvoretskyi && source venv/bin/activate
alembic current                      # should be the latest revision (head)
alembic history --verbose | head
ls -la dvoretskyi.db*                 # .bak.<epoch> snapshots from each deploy
# restore a backup if a migration went wrong:
#   sudo systemctl stop dvoretskyi && cp dvoretskyi.db.bak.<epoch> dvoretskyi.db && sudo systemctl start dvoretskyi
```

**Webhook (mono) reachability** — needs nginx + TLS up:
```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://dvoretskyi.duckdns.org/health
sudo nginx -t && sudo systemctl reload nginx
sudo journalctl -u nginx -n 30 --no-pager
```

After any fix: `sudo systemctl restart dvoretskyi && systemctl is-active dvoretskyi`.
