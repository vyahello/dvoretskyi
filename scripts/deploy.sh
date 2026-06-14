#!/usr/bin/env bash
# Idempotent deploy — runs ON THE VPS (by CI over SSH, or by hand).
# Pulls main cleanly, migrates, seeds, restarts the service.
set -euo pipefail

APP_DIR="/home/cax/dvoretskyi"
cd "$APP_DIR"

echo "==> Fetch + hard reset to origin/main"
git fetch --all
git reset --hard origin/main

if [ ! -d venv ]; then
  echo "==> Creating venv (first run)"
  python3 -m venv venv
fi

echo "==> Activate venv + install"
# shellcheck source=/dev/null
source venv/bin/activate
pip install -e ".[dev]"

if [ -f dvoretskyi.db ]; then
  backup="dvoretskyi.db.bak.$(date +%s)"
  echo "==> Backup DB -> $backup"
  cp dvoretskyi.db "$backup"
fi

echo "==> Alembic upgrade"
alembic upgrade head

echo "==> Seed providers (idempotent)"
dvoretskyi seed-providers

echo "==> Restart service"
sudo systemctl restart dvoretskyi

echo "==> Health: systemctl is-active"
systemctl is-active dvoretskyi   # non-'active' exits non-zero -> set -e fails the deploy
