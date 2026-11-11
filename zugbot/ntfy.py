"""Benachrichtigungen über ntfy (https://ntfy.sh)."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime

import requests

from . import config

log = logging.getLogger(__name__)

PRIO = {"niedrig": "2", "normal": "3", "hoch": "4", "dringend": "5"}


MELDUNGEN = config.DATEN_DIR / "meldungen.json"
_lock = threading.Lock()


def _merken(titel: str, text: str, prio: str) -> None:
    """Die letzten Pushes für das Dashboard ("Meldungen heute")."""
    try:
        with _lock:
            alt = meldungen()
            alt.append({"um": datetime.now(config.zeitzone()).isoformat(timespec="seconds"),
                        "titel": titel, "text": text, "prio": prio})
            tmp = MELDUNGEN.with_suffix(".tmp")
            tmp.write_text(json.dumps(alt[-40:], ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, MELDUNGEN)
    except OSError as exc:
        log.warning("Meldung nicht gemerkt: %s", exc)


def meldungen() -> list[dict]:
    try:
        return json.loads(MELDUNGEN.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


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
        _merken(titel, text, prio)
        return True
    except requests.RequestException as exc:
        log.error("ntfy fehlgeschlagen: %s", exc)
        return False
