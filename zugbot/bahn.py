"""Zugriff auf Transitous (MOTIS-2-API, https://transitous.org).

Warum Transitous und nicht db-rest: Die DB blockt db-vendo-client/db-rest
(OPS_BLOCKED), und der README von db-vendo-client verlangt eine Erlaubnis der DB.
Transitous ist ein offener Dienst aus GTFS/GTFS-RT-Daten (für Hessen über DELFI)
mit Echtzeit. Nutzungsregeln: https://transitous.org/api/
  - User-Agent mit Name, Version und Kontakt
  - keine Massenabfragen (über 720 /plan-Anfragen am Tag gilt als viel)
  - Quellenangabe sichtbar (steht im Dashboard)

- Kleiner Zwischenspeicher: gleiche Anfrage innerhalb von cache_sekunden
  wird nicht erneut gestellt.
- Ausfall-Erkennung: nach N Fehlversuchen in Folge gilt die Bahn als
  "nicht erreichbar". Der Dienst schickt dann genau EINE Meldung.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from . import config

log = logging.getLogger(__name__)


class BahnFehler(Exception):
    """Schnittstelle nicht erreichbar oder Serverfehler."""


class Bahn:
    def __init__(self, einstellungen: Optional[dict] = None):
        e = einstellungen or config.einstellungen()
        env = config.env()
        self.basis = env.transitous_url
        self.timeout = e["bahn"]["timeout_sekunden"]
        self.cache_s = e["bahn"]["cache_sekunden"]
        self.modi = e["transit_modi"]
        self.session = requests.Session()
        self.session.headers["User-Agent"] = env.user_agent
        self._cache: dict[str, tuple[float, object]] = {}
        self.fehler_in_folge = 0
        self.letzter_fehler = ""
        self.anfragen_heute: dict[str, int] = {}

    # --- intern ---
    def _get(self, pfad: str, params: dict, cache: bool = True):
        params = {k: v for k, v in params.items() if v is not None}
        schluessel = pfad + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        jetzt = time.monotonic()
        if cache and schluessel in self._cache:
            zeit, daten = self._cache[schluessel]
            if jetzt - zeit < self.cache_s:
                return daten
        tag = datetime.now().date().isoformat()
        self.anfragen_heute = {tag: self.anfragen_heute.get(tag, 0) + 1}
        try:
            r = self.session.get(self.basis + pfad, params=params, timeout=self.timeout)
            if r.status_code >= 500 or r.status_code == 429:
                raise BahnFehler(f"HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code >= 400:
                # 4xx = Anfrage passt nicht (z. B. veraltete Verbindungs-ID),
                # kein Ausfall der Schnittstelle.
                self.fehler_in_folge = 0
                raise ValueError(f"HTTP {r.status_code}: {r.text[:200]}")
            daten = r.json()
        except (requests.RequestException, BahnFehler) as exc:
            self.fehler_in_folge += 1
            self.letzter_fehler = str(exc)
            log.warning("Transitous-Fehler (%d in Folge): %s", self.fehler_in_folge, exc)
            raise BahnFehler(str(exc)) from exc
        self.fehler_in_folge = 0
        self._cache[schluessel] = (jetzt, daten)
        if len(self._cache) > 300:
            self._cache.clear()
        return daten

    # --- öffentlich ---
    def haltestelle_suchen(self, name: str) -> list[dict]:
        return self._get("/api/v1/geocode", {"text": name, "type": "STOP"}, cache=False)

    def verbindungen(self, von: str, nach: str, *, abfahrt: Optional[datetime] = None,
                     ankunft: Optional[datetime] = None, ueber: Optional[str] = None,
                     anzahl: int = 5, nur_modi: Optional[list[str]] = None) -> list[dict]:
        """Sucht Verbindungen. Genau eins von abfahrt/ankunft angeben.

        Zeiten gehen als UTC raus (MOTIS rechnet intern in UTC), die Antwort
        wird im Modell nach Europe/Berlin umgerechnet.
        """
        zeit = ankunft or abfahrt
        params = {
            "fromPlace": von, "toPlace": nach,
            "via": ueber,
            "time": zeit.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if zeit else None,
            "arriveBy": "true" if ankunft else "false",
            "numItineraries": anzahl,
            "searchWindow": 3600,          # eine Stunde Suchfenster: Züge fahren teils im Stundentakt
            "transitModes": ",".join(nur_modi or self.modi),
            "detailedLegs": "false",
            "language": "de",
        }
        return self._get("/api/v6/plan", params).get("itineraries", [])

    def aktualisieren(self, verbindungs_id: str) -> dict:
        """Holt Verspätungen und Ausfälle für eine gespeicherte Verbindung (refresh-itinerary)."""
        daten = self._get("/api/v6/refresh-itinerary",
                          {"itineraryId": verbindungs_id, "detailedLegs": "false", "language": "de"})
        return daten.get("itinerary", daten)
