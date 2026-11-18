"""Gleise, Verspätungen und Ausfälle für Züge aus der offiziellen DB-Timetables-API.

Transitous kennt bei Bus und Tram den Steig, bei Zügen fehlt das Gleis oft.
Die DB-Timetables-API (developers.deutschebahn.com, kostenloser Schlüssel)
liefert für jeden Bahnhof die Abfahrtstafel mit geplantem Gleis (pp) und
Änderungen (cp = geändertes Gleis, ct = geänderte Zeit, cs="c" = Ausfall).

Echtzeit für Züge kommt von hier, nicht von Transitous: Ein Vergleich am
5.11.2026 an unseren 4 Bahnhöfen ergab bei Transitous für 41 von 110 Zügen
gar keine Echtzeit und 5 zu kleine Verspätungen; die DB hatte alle. Ein
Tafeleintrag ohne ct heißt bei der DB: pünktlich.

Zuordnung eines Zugs zu einem Tafeleintrag: gleicher Bahnhof (EVA-Nummer aus
einstellungen.yaml), gleiche Planzeit (Minute genau), falls angegeben gleiche
Liniennummer (RE 50 -> "50", S6 -> "6") und die richtige Richtung. Die Richtung
ist nötig, weil z. B. in Darmstadt Hbf um 00:30 eine RB 68 nach Frankfurt und
eine nach Heidelberg fährt, auf verschiedenen Gleisen. Manche Züge stehen auch
doppelt in der Tafel, und die DB trägt Änderungen nur bei einem Eintrag ein.
Dann gilt der Eintrag mit Änderungen. Verbindung und Abfahrtstafel nutzen
dieselbe Auswahl (waehle), damit sie sich nie widersprechen.

Ohne DB_CLIENT_ID/DB_API_KEY in der .env tut dieses Modul nichts.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

import requests

from . import config
from .modell import TZ, Abschnitt, Verbindung, basis_id

log = logging.getLogger(__name__)

BASIS = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1"
ZUG_PRODUKTE = {"regional", "regionalExpress", "suburban"}
# Fernverkehr gehört nicht auf die Tafel (Deutschlandticket)
FERNVERKEHR = {"ICE", "IC", "EC", "ECE", "EN", "NJ", "RJ", "RJX", "TGV", "FLX", "D"}
PLAN_CACHE_S = 6 * 3600      # Fahrplan einer Stunde ändert sich kaum
AENDERUNG_CACHE_S = 90       # Änderungen (Gleiswechsel) frisch halten
# Die DB-Tafel reicht nur ca. 18 h voraus, weiter weg kommt 404. Eine leere
# Antwort deshalb nur kurz merken, sonst fehlen die Gleise unnötig lange.
LEER_CACHE_S = 30 * 60
# Ohne ct gilt ein Zug als pünktlich, aber erst kurz vor der Fahrt: Für den Zug
# morgen früh heißt "keine Änderung bekannt" noch nicht "pünktlich".
PUENKTLICH_AB = timedelta(minutes=90)


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
    @staticmethod
    def waehle(kandidaten: list[ET.Element]) -> Optional[ET.Element]:
        """Von mehreren passenden Einträgen den mit den meisten Änderungen
        (Ausfall, Zeit, Gleis); bei Gleichstand den ersten."""
        if not kandidaten:
            return None
        return max(kandidaten, key=lambda e: sum(bool(e.get(k)) for k in ("cs", "ct", "cp")))

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
        """'RE 50' -> '50', 'RE60 (4672)' -> '60' (Zugnummer in Klammern weg)."""
        m = re.search(r"(\d+)\s*$", re.sub(r"\s*\(.*?\)\s*$", "", linie or ""))
        return m.group(1) if m else ""

    def _finde(self, eva: str, zeit: datetime, art: str, linie: str,
               anderes_ende: str = "") -> Optional[ET.Element]:
        """art "dp": anderes_ende ist dein Ausstieg, er muss auf dem weiteren Weg
        liegen. art "ar": dein Einstieg, er muss auf dem bisherigen Weg liegen."""
        treffer = self._finde_eintrag(eva, zeit, art, linie, anderes_ende)
        return treffer.find(art) if treffer is not None else None

    def _finde_eintrag(self, eva: str, zeit: datetime, art: str, linie: str,
                       anderes_ende: str = "") -> Optional[ET.Element]:
        pt = zeit.strftime("%y%m%d%H%M")
        nummer = self._liniennummer(linie)
        kandidaten = []
        for s in self._eintraege(eva, zeit).values():
            e = s.find(art)
            if e is None or e.get("pt") != pt:
                continue
            # DB schreibt "RE50", "S6"; wir haben "RE 50", "S6". Nur die Ziffern
            # vergleichen. Wichtig, weil z. B. RE50 und RB46 in Gelnhausen beide 06:37 fahren.
            if nummer and e.get("l") and self._liniennummer(e.get("l")) != nummer:
                continue
            pfad = _pfad(e)
            if anderes_ende and pfad and not any(_gleicher_halt(anderes_ende, h) for h in pfad):
                continue      # Gegenrichtung
            kandidaten.append(s)
        gewaehlt = self.waehle([s.find(art) for s in kandidaten])
        return next((s for s in kandidaten if s.find(art) is gewaehlt), None)

    def eintrag_id(self, eva: str, a: Abschnitt) -> Optional[str]:
        """Id des Tafeleintrags, der zur Abfahrt von a gehört (für "dein Zug")."""
        s = self._finde_eintrag(eva, a.ab_plan, "dp", a.linie, a.nach.name)
        return s.get("id") if s is not None else None

    def tafel(self, eva: str, von: datetime, bis: datetime, ziele: list[str]) -> list[dict]:
        """Abfahrten an einem Bahnhof, nur Nahverkehr, der über einen der Ziel-
        bahnhöfe fährt (Namen wie bei Transitous). Mit Echtzeit."""
        def faehrt_hin(halt: str) -> bool:
            return any(_gleicher_halt(z, halt) for z in ziele)

        kandidaten: dict[tuple, list[tuple[ET.Element, str]]] = {}
        stunde = von.replace(minute=0, second=0, microsecond=0)
        while stunde <= bis:
            for s in self._eintraege(eva, stunde).values():
                dp, tl = s.find("dp"), s.find("tl")
                if dp is None or not dp.get("pt"):
                    continue
                kat = tl.get("c", "") if tl is not None else ""
                if kat in FERNVERKEHR:
                    continue
                plan = _db_zeit(dp.get("pt"))
                ist = _db_zeit(dp.get("ct")) or plan
                if ist < von or plan > bis:
                    continue
                ueber = next((h for h in _pfad(dp) if faehrt_hin(h)), None)
                if ueber is None:
                    continue
                linie = _linie(kat, dp.get("l") or "")
                # Doppelte Einträge (gleiche Minute, Linie, Ziel): einer reicht, wie in _finde gewählt
                schluessel = (dp.get("pt"), linie, (_pfad(dp) or [""])[-1])
                kandidaten.setdefault(schluessel, []).append((s, ueber))
            stunde += timedelta(hours=1)
        zeilen = []
        for liste in kandidaten.values():
            dp = self.waehle([s.find("dp") for s, _ in liste])
            s, ueber = next((s, u) for s, u in liste if s.find("dp") is dp)
            plan = _db_zeit(dp.get("pt"))
            ist = _db_zeit(dp.get("ct")) or plan
            pfad = _pfad(dp)
            zeilen.append({
                "id": s.get("id"), "pt": dp.get("pt"), "plan": f"{plan:%H:%M}", "ist": f"{ist:%H:%M}",
                "plus": round((ist - plan).total_seconds() / 60), "ausfall": dp.get("cs") == "c",
                "linie": linie_von(s, dp), "ziel": pfad[-1] if pfad else "", "ueber": ueber,
                "gleis": dp.get("cp") or dp.get("pp") or "",
                "gleis_plan": dp.get("pp") or "",
            })
        return sorted(zeilen, key=lambda z: (z["ist"], z["plan"]))

    def ergaenzen(self, v: Optional[Verbindung]) -> None:
        """Trägt Gleise und Echtzeit in alle Zug-Abschnitte ein (verändert v direkt)."""
        if not self.aktiv or v is None:
            return
        for a in v.fahrten:
            if a.produkt not in ZUG_PRODUKTE:
                continue
            self._abschnitt(a)

    def _abschnitt(self, a: Abschnitt) -> None:
        eva_ab = self.eva.get(a.von.basis)
        if eva_ab:
            dp = self._finde(eva_ab, a.ab_plan, "dp", a.linie, a.nach.name)
            if dp is not None:
                a.gleis_ab_plan = dp.get("pp") or a.gleis_ab_plan
                a.gleis_ab = dp.get("cp") or dp.get("pp") or a.gleis_ab
                a.ab_ist = _db_zeit(dp.get("ct")) or (a.ab_plan if _nah(a.ab_plan) else a.ab_ist)
                a.ausfall = dp.get("cs") == "c"
                a.quelle = "DB"
        an_db = None
        eva_an = self.eva.get(a.nach.basis)
        if eva_an:
            ar = self._finde(eva_an, a.an_plan, "ar", a.linie, a.von.name)
            if ar is not None:
                a.gleis_an_plan = ar.get("pp") or a.gleis_an_plan
                a.gleis_an = ar.get("cp") or ar.get("pp") or a.gleis_an
                an_db = _db_zeit(ar.get("ct"))
                a.an_ist = an_db or (a.an_plan if _nah(a.ab_plan) else a.an_ist)
                a.ausfall = a.ausfall or ar.get("cs") == "c"
                a.quelle = "DB"
        # Fährt der Zug verspätet ab, kommt er nicht pünktlich an. Meldet die DB
        # für die Ankunft (noch) nichts, die Abfahrtsverspätung übernehmen.
        if an_db is None and a.ab_ist and a.ab_ist > a.ab_plan:
            mindestens = a.an_plan + (a.ab_ist - a.ab_plan) - timedelta(minutes=3)  # etwas Aufholen
            if a.an_ist is None or a.an_ist < mindestens:
                a.an_ist = max(mindestens, a.an_plan)


def _pfad(e: ET.Element) -> list[str]:
    """Halte vor (ar) bzw. nach (dp) diesem Bahnhof, mit Änderungen."""
    pfad = e.get("cpth") or e.get("ppth") or ""
    return pfad.split("|") if pfad else []


def _gleicher_halt(a: str, b: str) -> bool:
    """'Gelnhausen Bahnhof' (Transitous) und 'Gelnhausen' (DB) sind derselbe Halt."""
    from .bewertung import _norm
    x, y = _norm(a), _norm(b)
    return bool(x and y) and (x.startswith(y) or y.startswith(x))


def linie_von(s: ET.Element, dp: ET.Element) -> str:
    tl = s.find("tl")
    return _linie(tl.get("c", "") if tl is not None else "", dp.get("l") or "")


def _db_zeit(wert: Optional[str]) -> Optional[datetime]:
    """DB-Zeitformat yyMMddHHmm, Ortszeit."""
    if not wert:
        return None
    return datetime.strptime(wert, "%y%m%d%H%M").replace(tzinfo=TZ)


def _nah(ab_plan: datetime) -> bool:
    return ab_plan - datetime.now(TZ) <= PUENKTLICH_AB


def _linie(kat: str, l: str) -> str:
    """Wie Transitous schreiben: "RB 82", "RE 50", "S6". Die DB nennt teils den
    Betreiber als Kategorie (VIA, HLB), l ist dann nur die Nummer."""
    m = re.match(r"^([A-Z]+)\s*(\d+)$", l.strip())
    if m:
        kat, nummer = m.group(1), m.group(2)
    else:
        nummer = re.sub(r"\D", "", l)
        if kat not in ("RE", "RB", "S", "IRE"):
            kat = "RB"     # Betreiberkürzel -> Regionalbahn
    return f"S{nummer}" if kat == "S" else f"{kat} {nummer}".strip()
