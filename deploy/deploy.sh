#!/bin/bash
# Vom Mac aus: Code auf den Pi kopieren und dort installieren.
#   ./deploy/deploy.sh            kopieren + installieren (Dienst wird neu gestartet, falls aktiv)
#   ./deploy/deploy.sh --starten  zusätzlich Dienst aktivieren und starten
# .env, daten/ und venv auf dem Pi bleiben unangetastet.
set -euo pipefail

PI="${PI:-bene635@tradingbot.tailed8a6b.ts.net}"
HIER="$(cd "$(dirname "$0")/.." && pwd)"

echo "Kopiere nach $PI:~/zugbot ..."
rsync -a --delete \
  --exclude '.git/' --exclude '.venv/' --exclude 'venv/' --exclude '__pycache__/' \
  --exclude '.env' --exclude 'daten/' --exclude '.claude/' --exclude '*.egg-info/' \
  --exclude '.pytest_cache/' \
  "$HIER/" "$PI:zugbot/"

ssh "$PI" "bash ~/zugbot/deploy/install.sh $*"
