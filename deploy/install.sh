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
# pip nur, wenn sich pyproject.toml geändert hat (neue Abhängigkeiten). Das
# editable install zeigt schon auf den Code, für reine Code-Änderungen reicht
# ein Neustart. Schlägt pip fehl (z. B. kein Internet), trotzdem weitermachen.
STAND="$(sha256sum pyproject.toml | cut -d' ' -f1)"
if [ "$(cat venv/.pyproject-stand 2>/dev/null)" != "$STAND" ]; then
  if venv/bin/pip install --quiet --disable-pip-version-check -e .; then
    echo "$STAND" > venv/.pyproject-stand
  else
    echo "WARNUNG: pip fehlgeschlagen, der Code ist trotzdem aktuell. Neue Abhängigkeiten fehlen evtl."
  fi
fi

# 2) .env anlegen, falls es sie noch nicht gibt (mit zufälligem ntfy-Topic)
if [ ! -f .env ]; then
  TOPIC="zugbot-$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
  sed -e "s|^NTFY_TOPIC=.*|NTFY_TOPIC=$TOPIC|" .env.example > .env
  chmod 600 .env
  echo ".env angelegt (ntfy-Topic: siehe 'grep NTFY_TOPIC ~/zugbot/.env')"
fi
# Dashboard-Schlüssel nachtragen, falls er fehlt (auch bei älteren .env)
if ! grep -q '^DASHBOARD_SCHLUESSEL=.\+' .env; then
  SCHLUESSEL="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  sed -i '/^DASHBOARD_SCHLUESSEL=/d' .env
  echo "DASHBOARD_SCHLUESSEL=$SCHLUESSEL" >> .env
  echo "Dashboard-Schlüssel angelegt (siehe 'zugbot dashboard-url')"
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
