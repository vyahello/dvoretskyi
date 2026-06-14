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
#    Do NOT add ANTHROPIC_API_KEY — the systemd unit pins it empty.
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
