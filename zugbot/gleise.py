"""Gleise (und Gleiswechsel) für Züge aus der offiziellen DB-Timetables-API.

Transitous kennt bei Bus und Tram den Steig, bei Zügen fehlt das Gleis oft.
Die DB-Timetables-API (developers.deutschebahn.com, kostenloser Schlüssel)
liefert für jeden Bahnhof die Abfahrtstafel mit geplantem Gleis (pp) und
Änderungen (cp = geändertes Gleis, cs="c" = Ausfall).

Zuordnung eines Zugs zu einem Tafeleintrag: gleicher Bahnhof (EVA-Nummer aus
einstellungen.yaml), gleiche Planzeit (Minute genau) und, falls angegeben,
gleiche Liniennummer (RE 50 -> "50", S6 -> "6").

Ohne DB_CLIENT_ID/DB_API_KEY in der .env tut dieses Modul nichts.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional
from urllib.parse import quote

import requests

from . import config
from .modell import Abschnitt, Verbindung, basis_id

log = logging.getLogger(__name__)

BASIS = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1"
ZUG_PRODUKTE = {"regional", "regionalExpress", "suburban"}
PLAN_CACHE_S = 6 * 3600      # Fahrplan einer Stunde ändert sich kaum
AENDERUNG_CACHE_S = 90       # Änderungen (Gleiswechsel) frisch halten
# Die DB-Tafel reicht nur ca. 18 h voraus, weiter weg kommt 404. Eine leere
# Antwort deshalb nur kurz merken, sonst fehlen die Gleise unnötig lange.
LEER_CACHE_S = 30 * 60


class DbGleise:
    def __init__(self, einstellungen: Optional[dict] = None):
        e = einstellungen or config.einstellungen()
        env = config.env()
        self.aktiv = bool(env.db_client_id and env.db_api_key)
        self.session = requests.Session()
        self.session.headers.update({
            "DB-Client-Id": env.db_client_id, "DB-Api-Key": env.db_api_key,
            "Accept": "application/xml", "User-Agent": env.user_agent,
        })
        # Transitous-Halt -> EVA-Nummer
        self.eva = {basis_id(str(h["id"])): str(h["eva"])
                    for h in e["haltestellen"].values() if h.get("id") and h.get("eva")}
        self._cache: dict[str, tuple[float, Optional[ET.Element]]] = {}

    def _xml(self, pfad: str, max_alter: float) -> Optional[ET.Element]:
        jetzt = time.monotonic()
        if pfad in self._cache:
            zeit, alt = self._cache[pfad]
            if jetzt - zeit < (max_alter if alt is not None else min(max_alter, LEER_CACHE_S)):
                return alt
        try:
            r = self.session.get(BASIS + pfad, timeout=20)
            # 404 = keine Fahrten in dieser Stunde, kein Fehler
            wurzel = ET.fromstring(r.content) if r.status_code == 200 and r.content else None
            if r.status_code not in (200, 404):
                log.warning("DB-Timetables HTTP %s für %s", r.status_code, pfad)
        except (requests.RequestException, ET.ParseError) as exc:
            log.warning("DB-Timetables nicht erreichbar: %s", exc)
            return None   # Gleise sind nur Zusatz, nicht cachen, später erneut versuchen
        self._cache[pfad] = (jetzt, wurzel)
        if len(self._cache) > 200:
            self._cache.clear()
        return wurzel

    @staticmethod
    def _db_schreibweisen(name: str) -> list[str]:
        """Transitous: 'Frankfurt (Main) Hauptbahnhof', DB: 'Frankfurt(Main)Hbf'."""
        db = (name.replace(" (Main) ", "(Main)").replace("Hauptbahnhof", "Hbf")
              .replace("Südbahnhof", "Süd").removesuffix(" Bahnhof"))
        varianten = [name, db, db.replace("(Main)", "(Main) "), name.removesuffix(" Bahnhof")]
        return list(dict.fromkeys(varianten))   # Reihenfolge behalten, doppelte weg

    def eva_suchen(self, name: str) -> Optional[tuple[str, str]]:
        """Gibt (EVA-Nummer, DB-Name) zurück oder None."""
        for versuch in self._db_schreibweisen(name):
            wurzel = self._xml("/station/" + quote(versuch, safe=""), 0)
            st = wurzel.find("station") if wurzel is not None else None
            if st is not None:
                return st.get("eva"), st.get("name")
        return None

    # --- Zuordnung ---
    def _eintraege(self, eva: str, zeit: datetime) -> dict[str, ET.Element]:
        """Alle Tafeleinträge (s-Elemente) der Stunde, mit eingemischten Änderungen."""
        plan = self._xml(f"/plan/{eva}/{zeit:%y%m%d}/{zeit:%H}", PLAN_CACHE_S)
        if plan is None:
            return {}
        eintraege = {s.get("id"): s for s in plan.findall("s")}
        aenderungen = self._xml(f"/fchg/{eva}", AENDERUNG_CACHE_S)
        if aenderungen is not None:
            for s in aenderungen.findall("s"):
                ziel = eintraege.get(s.get("id"))
                if ziel is None:
                    continue
                for art in ("ar", "dp"):
                    neu, alt = s.find(art), ziel.find(art)
                    if neu is not None and alt is not None:
                        for k in ("cp", "ct", "cs"):
                            if neu.get(k):
                                alt.set(k, neu.get(k))
        return eintraege

    @staticmethod
    def _liniennummer(linie: str) -> str:
        m = re.search(r"(\d+)\s*$", linie)
        return m.group(1) if m else ""

    def _finde(self, eva: str, zeit: datetime, art: str, linie: str) -> Optional[ET.Element]:
        pt = zeit.strftime("%y%m%d%H%M")
        nummer = self._liniennummer(linie)
        for s in self._eintraege(eva, zeit).values():
            e = s.find(art)
            if e is None or e.get("pt") != pt:
                continue
            # DB schreibt "RE50", "S6"; wir haben "RE 50", "S6". Nur die Ziffern
            # vergleichen. Wichtig, weil z. B. RE50 und RB46 in Gelnhausen beide 06:37 fahren.
            if nummer and e.get("l") and self._liniennummer(e.get("l")) != nummer:
                continue
            return e
        return None

    def ergaenzen(self, v: Optional[Verbindung]) -> None:
        """Trägt Gleise in alle Zug-Abschnitte ein (verändert v direkt)."""
        if not self.aktiv or v is None:
            return
        for a in v.fahrten:
            if a.produkt not in ZUG_PRODUKTE:
                continue
            self._abschnitt(a)

    def _abschnitt(self, a: Abschnitt) -> None:
        eva_ab = self.eva.get(a.von.basis)
        if eva_ab:
            dp = self._finde(eva_ab, a.ab_plan, "dp", a.linie)
            if dp is not None:
                a.gleis_ab_plan = dp.get("pp") or a.gleis_ab_plan
                a.gleis_ab = dp.get("cp") or dp.get("pp") or a.gleis_ab
                if dp.get("cs") == "c":
                    a.ausfall = True
        eva_an = self.eva.get(a.nach.basis)
        if eva_an:
            ar = self._finde(eva_an, a.an_plan, "ar", a.linie)
            if ar is not None:
                a.gleis_an_plan = ar.get("pp") or a.gleis_an_plan
                a.gleis_an = ar.get("cp") or ar.get("pp") or a.gleis_an
