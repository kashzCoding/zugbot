#!/bin/bash
# Auf dem Pi: venv anlegen/aktualisieren, systemd-Unit und logrotate einrichten.
# Aufruf durch deploy.sh, oder direkt: bash ~/zugbot/deploy/install.sh [--starten]
set -euo pipefail

cd "$HOME/zugbot"

# 1) venv. --system-site-packages, damit das schon installierte python3-requests
#    aus Debian genutzt wird (spart auf dem Pi Zero Zeit und Speicher).
if [ ! -x venv/bin/python ]; then
  echo "Lege venv an ..."
  python3 -m venv --system-site-packages venv
fi
venv/bin/pip install --quiet --disable-pip-version-check -e .

# 2) .env anlegen, falls es sie noch nicht gibt (mit zufälligem ntfy-Topic)
if [ ! -f .env ]; then
  TOPIC="zugbot-$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
  TSIP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
  sed -e "s|^NTFY_TOPIC=.*|NTFY_TOPIC=$TOPIC|" -e "s|^WEB_HOST=.*|WEB_HOST=${TSIP:-127.0.0.1}|" .env.example > .env
  chmod 600 .env
  echo ".env angelegt (ntfy-Topic: siehe 'grep NTFY_TOPIC ~/zugbot/.env')"
fi

# 3) Befehl "zugbot" überall verfügbar machen
sudo ln -sf "$HOME/zugbot/venv/bin/zugbot" /usr/local/bin/zugbot

# 4) Log-Verzeichnis, logrotate, systemd
sudo install -d -o "$USER" -g "$USER" -m 0755 /var/log/zugbot
sudo install -m 0644 deploy/logrotate-zugbot /etc/logrotate.d/zugbot
sudo install -m 0644 deploy/zugbot.service /etc/systemd/system/zugbot.service
sudo systemctl daemon-reload

if [ "${1:-}" = "--starten" ]; then
  sudo systemctl enable --now zugbot.service
  echo "zugbot läuft: systemctl status zugbot"
elif systemctl is-active --quiet zugbot.service; then
  sudo systemctl restart zugbot.service
  echo "zugbot neu gestartet."
else
  echo "Installiert, aber nicht gestartet. Starten mit: ./deploy/deploy.sh --starten"
fi
