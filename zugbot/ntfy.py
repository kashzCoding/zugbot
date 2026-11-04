"""Benachrichtigungen über ntfy (https://ntfy.sh)."""

from __future__ import annotations

import logging

import requests

from . import config

log = logging.getLogger(__name__)

PRIO = {"niedrig": "2", "normal": "3", "hoch": "4", "dringend": "5"}


def senden(titel: str, text: str, prio: str = "normal", tags: str = "") -> bool:
    env = config.env()
    if not env.ntfy_topic:
        log.warning("NTFY_TOPIC fehlt in .env, Nachricht nicht gesendet: %s", titel)
        return False
    try:
        # Titel als Header muss latin-1-kodierbar sein; Umlaute gehen über RFC 2047 nicht
        # zuverlässig, deshalb Titel im JSON-Format schicken.
        r = requests.post(env.ntfy_server, json={
            "topic": env.ntfy_topic, "title": titel, "message": text,
            "priority": int(PRIO.get(prio, "3")),
            "tags": [t for t in tags.split(",") if t],
        }, timeout=15, headers={"User-Agent": env.user_agent})
        r.raise_for_status()
        log.info("ntfy gesendet (%s): %s", prio, titel)
        return True
    except requests.RequestException as exc:
        log.error("ntfy fehlgeschlagen: %s", exc)
        return False
