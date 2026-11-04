"""Gespeicherter Zustand (daten/zustand.json): Pläne, gesendete Meldungen, Status.

JSON statt Datenbank, damit du bei der Fehlersuche einfach reinschauen kannst.
Geschrieben wird atomar (erst temporäre Datei, dann umbenennen), damit ein
Stromausfall keine halbe Datei hinterlässt.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date, timedelta
from typing import Optional

from . import config
from .modell import Verbindung

_lock = threading.Lock()
PFAD = config.DATEN_DIR / "zustand.json"


def laden() -> dict:
    with _lock:
        try:
            return json.loads(PFAD.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}


def speichern(z: dict) -> None:
    with _lock:
        PFAD.parent.mkdir(parents=True, exist_ok=True)
        tmp = PFAD.with_suffix(".tmp")
        tmp.write_text(json.dumps(z, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, PFAD)


def aufraeumen(z: dict, heute: date) -> None:
    """Alte Tage entfernen (älter als 3 Tage)."""
    grenze = (heute - timedelta(days=3)).isoformat()
    for bereich in ("plaene", "gemeldet", "gesendet"):
        for k in list(z.get(bereich, {})):
            if k[:10] < grenze:
                del z[bereich][k]


def plan(z: dict, d: date) -> Optional[dict]:
    return z.get("plaene", {}).get(d.isoformat())


def verbindung(z: dict, d: date, richtung: str) -> Optional[Verbindung]:
    p = plan(z, d)
    if not p or not p.get(richtung):
        return None
    return Verbindung.aus_dict(p[richtung])


def wahl_anwenden(p: dict, richtung: str, schluessel: str) -> bool:
    """Macht die Option mit diesem Schlüssel zur überwachten Verbindung.

    p["wahl"] merkt sich die zuletzt übernommene Wahl. Sonst würde der Dienst
    nach einer Neuplanung unterwegs wieder auf die alte Option zurückspringen.
    """
    p.setdefault("wahl", {})[richtung] = schluessel
    for o in p.get("optionen", {}).get(richtung, []):
        if Verbindung.aus_dict(o).schluessel() == schluessel:
            p[richtung] = o
            p.get("geprueft", {}).pop(richtung, None)   # gleich frisch prüfen
            return True
    return False
