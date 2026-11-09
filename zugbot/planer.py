"""Verbindungssuche für Hin- und Rückweg und Neuplanung unterwegs.

Transitous kann "nur Nahverkehr" und "über X", aber nicht
"nie über Babenhausen" oder "in Darmstadt Hbf mind. 7 min". Deshalb stellt
der Planer mehrere Anfragen und filtert/bewertet selbst (bewertung.py).

Lichtwiese:  eine Suche Gelnhausen -> Lichtwiese, je über Frankfurt Hbf und Süd,
             je zum Bahnhof und zum Tram-Halt Campus (4 Anfragen).
Stadtmitte:  zweistufig, weil der Umstieg in Darmstadt Hbf (mind. 7 min)
             zwischen zwei getrennt gesuchten Stücken liegt:
             Zug Gelnhausen <-> Darmstadt Hbf und Tram/Bus Darmstadt Hbf <-> Schloss/WBP.

Neben der besten Verbindung bleiben bis zu OPTIONEN_HOECHSTENS weitere zur
Auswahl (Dashboard). Dazu kommen alle direkten Trams/Busse Hbf <-> Campus im
Zeitfenster, für die Anzeige von Takt und Alternativen (stadtfahrten, takt.py).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from . import bewertung
from .bahn import Bahn, BahnFehler
from .bewertung import Regeln
from .modell import Abschnitt, Halt, Teil, Verbindung, _zeit, basis_id
from .stundenplan import Tag

log = logging.getLogger(__name__)

NAHVERKEHR_STADT = ["BUS", "TRAM"]   # für das Stück Hbf <-> Schloss/WBP


@dataclass
class Ergebnis:
    verbindung: Optional[Verbindung]
    kandidaten: int = 0
    verworfen: list[str] = field(default_factory=list)   # Gründe, für "zugbot plan --details"
    optionen: list[Verbindung] = field(default_factory=list)   # beste zuerst, inkl. verbindung


class Planer:
    def __init__(self, bahn: Bahn, einstellungen: dict):
        from .gleise import DbGleise

        self.bahn = bahn
        self.e = einstellungen
        self.regeln = Regeln.aus_einstellungen(einstellungen)
        self.gleise = DbGleise(einstellungen)
        self.opt = {"hoechstens": 6, "zeitfenster_minuten": 90, "laenger_hoechstens_minuten": 40,
                    **einstellungen.get("optionen", {})}

    # ------------------------------------------------------------ Hilfen
    def hid(self, schluessel: str) -> str:
        h = self.e["haltestellen"][schluessel]
        if not h.get("id"):
            raise RuntimeError(f"Haltestelle {schluessel} hat keine ID. "
                               "Einmal 'zugbot haltestellen --schreiben' ausführen.")
        return str(h["id"])

    def campus_halte(self, campus: str) -> list[str]:
        return [self.hid(k) for k in self.e["campus"][campus]["halte"]]

    def ueber_halte(self) -> list[str]:
        return [self.hid(k) for k in self.e["pflicht_ueber"]]

    @staticmethod
    def _direkt_am_halt(t: Teil, halt_id: str, ende: bool) -> bool:
        """Stadtmitte: Tram/Bus soll direkt an Schloss bzw. Willy-Brandt-Platz
        abfahren (Rückweg) oder ankommen (Hinweg), nicht erst nach einem Fußweg
        zu einem anderen Halt."""
        fahrt = _letzte_fahrt(t) if ende else _erste_fahrt(t)
        if fahrt is None:
            return False
        return (fahrt.nach if ende else fahrt.von).basis == basis_id(halt_id)

    def _suche(self, *args, **kwargs) -> list[Teil]:
        try:
            return [Teil.aus_motis(j) for j in self.bahn.verbindungen(*args, **kwargs)]
        except ValueError as exc:     # z. B. "keine Verbindung gefunden"
            log.info("Suche ohne Ergebnis: %s", exc)
            return []

    def _waehle(self, kandidaten: list[Verbindung], punkte: Callable[[Verbindung], float],
                bedingung: Callable[[Verbindung], Optional[str]],
                pflicht_frankfurt: bool = True) -> Ergebnis:
        gesehen, gueltig, verworfen = set(), [], []
        for v in kandidaten:
            if v.schluessel() in gesehen:
                continue
            gesehen.add(v.schluessel())
            gruende = bewertung.pruefe(v, self.regeln, pflicht_frankfurt)
            extra = bedingung(v)
            if extra:
                gruende.append(extra)
            if gruende:
                verworfen.append(f"{_kurz(v)}: {'; '.join(gruende)}")
            else:
                gueltig.append(v)
        gueltig.sort(key=punkte, reverse=True)
        optionen = self._sinnvolle(_verschiedene_zuege(gueltig))
        for v in optionen:
            self.gleise.ergaenzen(v)   # Zuggleise aus der DB-API, falls Schlüssel vorhanden
        return Ergebnis(optionen[0] if optionen else None, len(gesehen), verworfen, optionen)

    def _sinnvolle(self, sortiert: list[Verbindung]) -> list[Verbindung]:
        """Optionen zur Auswahl: zeitlich nah an der besten und nicht viel länger."""
        if not sortiert:
            return []
        beste = sortiert[0]
        fenster = timedelta(minutes=self.opt["zeitfenster_minuten"])
        laenger = self.opt["laenger_hoechstens_minuten"]
        return [v for v in sortiert
                if v is beste or (abs(v.ab_plan - beste.ab_plan) <= fenster
                                  and v.dauer_min <= beste.dauer_min + laenger)][:self.opt["hoechstens"]]

    # ------------------------------------------------------------ Hinweg
    def ziel_zeit(self, tag: Tag) -> datetime:
        h = self.e["hinweg"]
        puffer = h["ankunftspuffer_minuten"] + h["gehzeit_minuten"].get(tag.campus, 0)
        # eigene Ankunftszeit zählt wie ein Vorlesungsbeginn (gleicher Puffer)
        return (tag.ziel_beginn or tag.erster.von) - timedelta(minutes=puffer)

    def hinweg(self, tag: Tag) -> Ergebnis:
        ziel = self.ziel_zeit(tag)
        if tag.campus == "lichtwiese":
            kandidaten = [
                Verbindung([t], "hin", tag.campus, ziel)
                for z in self.campus_halte("lichtwiese")
                for ueber in self.ueber_halte()
                for t in self._suche(self.hid("gelnhausen"), z, ankunft=ziel, ueber=ueber, anzahl=4)
            ]
        else:
            kandidaten = self._stadtmitte_hin(ziel)
        return self._waehle(
            kandidaten,
            lambda v: bewertung.punkte_hin(v, self.regeln),
            lambda v: None if v.an_plan <= ziel and v.an <= ziel else f"Ankunft {v.an:%H:%M} zu spät",
        )

    def _stadtmitte_hin(self, ziel: datetime) -> list[Verbindung]:
        da = self.hid("darmstadt_hbf")
        # 1) Tram/Bus ab Darmstadt Hbf, die rechtzeitig an Schloss/WBP sind
        stadt = [t for z in self.campus_halte("stadtmitte")
                 for t in self._suche(da, z, ankunft=ziel, anzahl=4, nur_modi=NAHVERKEHR_STADT)
                 if self._direkt_am_halt(t, z, ende=True)]
        # 2) Züge Gelnhausen -> Darmstadt Hbf, mit Luft für Umstieg + Tramfahrt
        zuege = [t for ueber in self.ueber_halte()
                 for t in self._suche(self.hid("gelnhausen"), da, ankunft=ziel - timedelta(minutes=12),
                                      ueber=ueber, anzahl=4)]
        return _kombiniere(zuege, stadt, "hin", "stadtmitte", ziel, self.regeln)

    # ------------------------------------------------------------ Rückweg
    def frueheste_rueckfahrt(self, tag: Tag) -> datetime:
        if tag.rueck_ab:        # eigene Zeit: ab dann an der Haltestelle
            return tag.rueck_ab
        return tag.letzter.bis + timedelta(minutes=self.e["rueckweg"]["frueheste_abfahrt_nach_ende_minuten"])

    def rueckweg(self, tag: Tag) -> Ergebnis:
        ab = self.frueheste_rueckfahrt(tag)
        if tag.campus == "lichtwiese":
            kandidaten = [
                Verbindung([t], "rueck", tag.campus)
                for start in self.campus_halte("lichtwiese")
                for ueber in self.ueber_halte()
                for t in self._suche(start, self.hid("gelnhausen"), abfahrt=ab, ueber=ueber, anzahl=3)
            ]
        else:
            kandidaten = self._stadtmitte_rueck(ab)
        return self._waehle(
            kandidaten,
            lambda v: bewertung.punkte_rueck(v, self.regeln),
            lambda v: None if v.ab_plan >= ab else f"Abfahrt {v.ab_plan:%H:%M} vor Ende",
        )

    def _stadtmitte_rueck(self, ab: datetime) -> list[Verbindung]:
        da = self.hid("darmstadt_hbf")
        starts = self.campus_halte("stadtmitte")
        mindest = self.regeln.umstieg_darmstadt_min
        # 1) Wann bin ich frühestens am Hbf?
        erste = [t for s in starts
                 for t in self._suche(s, da, abfahrt=ab, anzahl=2, nur_modi=NAHVERKEHR_STADT)
                 if self._direkt_am_halt(t, s, ende=False)]
        if not erste:
            return []
        frueh = min(t.abschnitte[-1].an for t in erste if t.abschnitte)
        # 2) Bester Zug ab Darmstadt Hbf Richtung Frankfurt
        zuege = [t for ueber in self.ueber_halte()
                 for t in self._suche(da, self.hid("gelnhausen"),
                                      abfahrt=frueh + timedelta(minutes=mindest), ueber=ueber, anzahl=3)]
        zuege = [z for z in zuege if not bewertung.pruefe(Verbindung([z], "rueck", "stadtmitte"), self.regeln)]
        zuege.sort(key=lambda z: bewertung.punkte_rueck(Verbindung([z], "rueck", "stadtmitte"), self.regeln),
                   reverse=True)
        # 3) Passende Tram/Bus zu den besten zwei Zügen
        kandidaten = []
        for zug in zuege[:2]:
            zug_ab = next(a for a in zug.abschnitte if not a.fussweg).ab
            stadt = [t for s in starts
                     for t in self._suche(s, da, ankunft=zug_ab - timedelta(minutes=mindest),
                                          anzahl=3, nur_modi=NAHVERKEHR_STADT)
                     if self._direkt_am_halt(t, s, ende=False)]
            stadt = [t for t in stadt if _erste_fahrt(t) and _erste_fahrt(t).ab_plan >= ab]
            kandidaten += _kombiniere(stadt, [zug], "rueck", "stadtmitte", None, self.regeln)
        return kandidaten

    # ------------------------------------------------------------ Letzte Meile
    def stadtfahrten(self, richtung: str, campus: str, optionen: list[Verbindung],
                     frueheste: Optional[datetime] = None) -> Optional[dict]:
        """Alle direkten Trams/Busse zwischen Darmstadt Hbf und Campus im Zeitfenster
        der Optionen. Daraus berechnet takt.py, was zu welcher Verbindung passt.

        Eine Suche pro Campus-Halt (campus.<name>.takt_halte), Fenster passend
        zu den Optionen, höchstens 3 Stunden.
        """
        umstieg = timedelta(minutes=self.regeln.umstieg_darmstadt_min)
        zeiten = []
        for v in optionen:
            for a in v.fahrten:
                if a.produkt in ("tram", "bus"):
                    continue
                if richtung == "hin" and bewertung.ist_darmstadt_hbf(a.nach, self.regeln):
                    zeiten.append(a.an_plan + umstieg)
                elif richtung == "rueck" and bewertung.ist_darmstadt_hbf(a.von, self.regeln):
                    zeiten.append(a.ab_plan - umstieg)
        if not zeiten:
            return None
        if richtung == "hin":
            start, ende = min(zeiten), max(v.ziel_zeit or max(zeiten) for v in optionen)
        else:
            start, ende = frueheste or min(zeiten) - timedelta(minutes=30), max(zeiten)
        fenster = int(min(3 * 3600, max(3600, (ende - start).total_seconds())))
        da = self.hid("darmstadt_hbf")
        c = self.e["campus"][campus]
        halte = [self.hid(k) for k in c.get("takt_halte", c["halte"])]
        gesehen, fahrten = set(), []
        for halt in halte:
            von, nach = (da, halt) if richtung == "hin" else (halt, da)
            for t in self._suche(von, nach, abfahrt=start, anzahl=12,
                                 nur_modi=NAHVERKEHR_STADT, fenster_s=fenster):
                f = [a for a in t.abschnitte if not a.fussweg]
                # nur ohne Umstieg, direkt ab/an Hbf und Campus-Halt
                if len(f) != 1 or not self._direkt_am_halt(t, von, ende=False) \
                        or not self._direkt_am_halt(t, nach, ende=True):
                    continue
                a = f[0]
                if (a.linie, a.ab_plan) in gesehen:
                    continue
                gesehen.add((a.linie, a.ab_plan))
                fahrten.append({"ab": a.ab_plan.isoformat(), "an": a.an_plan.isoformat(), "linie": a.linie,
                                "von": a.von.name, "nach": a.nach.name, "steig": a.gleis_ab})
        fahrten.sort(key=lambda x: x["ab"])
        return {"ab": frueheste.isoformat() if frueheste else None, "fahrten": fahrten}

    # ------------------------------------------------------------ Bestimmter Zug
    def ab_zug(self, v: Verbindung, halt_id: str, linie: str, ab_plan: datetime,
               jetzt: datetime) -> Optional[Verbindung]:
        """Du nimmst einen bestimmten Zug (von der Abfahrtstafel): den Rest der
        Fahrt ab diesem Zug planen. Bereits gefahrene Abschnitte bleiben vorne."""
        nummer = _nummer(linie)
        fahrten = v.fahrten
        idx = next((i for i, a in enumerate(fahrten) if a.von.basis == basis_id(halt_id)), len(fahrten))
        vorne = fahrten[:idx]
        ziele = self.campus_halte(v.campus) if v.richtung == "hin" else [self.hid("gelnhausen")]
        kandidaten = []
        for z in ziele:
            for t in self._suche(halt_id, z, abfahrt=ab_plan - timedelta(minutes=1), anzahl=5):
                erste = _erste_fahrt(t)
                if erste is None or erste.ab_plan != ab_plan or _nummer(erste.linie) != nummer:
                    continue
                teile = ([Teil(vorne)] if vorne else []) + [t]
                kandidaten.append(Verbindung(teile, v.richtung, v.campus, v.ziel_zeit, neu_geplant=True))
        if not kandidaten:
            # Die Suche liefert nur die "besten" Verbindungen. Fährt kurz danach
            # etwas Besseres (S6 17:05, RE 60 17:07), kommt dein Zug nie vor.
            # Dann die Fahrt selbst holen und ab ihren Umstiegsbahnhöfen weitersuchen.
            kandidaten = self._ueber_fahrt(v, vorne, halt_id, nummer, ab_plan, ziele)
        for k in kandidaten:
            self.gleise.ergaenzen(k)
        gut = [k for k in kandidaten if not bewertung.pruefe(k, self.regeln, pflicht_frankfurt=False)]
        if not gut:      # Anschluss nach Live-Stand weg: ab dort neu planen
            gut = [r for r in (self.neu_planen(k, jetzt) for k in kandidaten) if r]
        if not gut:
            return None
        beste = max(gut, key=lambda x: bewertung.punkte_rettung(x, self.regeln, v.ziel_zeit))
        beste.hinweis = f"Du nimmst {linie} {ab_plan:%H:%M}"
        return beste

    def _ueber_fahrt(self, v: Verbindung, vorne: list[Abschnitt], halt_id: str, nummer: str,
                     ab_plan: datetime, ziele: list[str]) -> list[Verbindung]:
        """Kandidaten "dein Zug bis X, ab X weiter" für die Umstiegsbahnhöfe X auf
        seinem Weg: Halte aus den Einstellungen und aus deinem bisherigen Plan
        (auch "Frankfurt Hbf tief" zu "Frankfurt Hbf"), oder direkt dein Ziel."""
        try:
            abfahrten = self.bahn.abfahrten(halt_id, ab_plan - timedelta(minutes=1))
        except (BahnFehler, ValueError) as exc:
            log.info("Abfahrten nicht abrufbar: %s", exc)
            return []
        trip = next((s.get("tripId") for s in abfahrten
                     if _zeit((s.get("place") or {}).get("scheduledDeparture")) == ab_plan
                     and _nummer(s.get("routeShortName") or s.get("displayName") or "") == nummer), None)
        if not trip:
            return []
        try:
            legs = self.bahn.fahrt(trip).get("legs") or []
        except (BahnFehler, ValueError) as exc:
            log.info("Fahrt %s nicht abrufbar: %s", trip, exc)
            return []
        if not legs:
            return []
        leg = legs[0]
        bekannt = {basis_id(h["id"]) for h in self.e["haltestellen"].values() if h.get("id")}
        bekannt |= {a.von.basis for a in v.fahrten} | {a.nach.basis for a in v.fahrten}
        namen = [h["name"] for h in self.e["haltestellen"].values()] + [a.nach.name for a in v.fahrten]
        ziel_basen = {basis_id(z) for z in ziele}
        # Die Fahrt kommt ab ihrem Startbahnhof, ab deinem Einstieg zuschneiden
        halte = [leg.get("from") or {}] + (leg.get("intermediateStops") or []) + [leg.get("to") or {}]
        start = next((i for i, s in enumerate(halte) if basis_id(s.get("stopId", "")) == basis_id(halt_id)), None)
        if start is None:
            return []
        einstieg = halte[start]
        kandidaten = []
        for i, s in enumerate(halte):
            if i <= start or not s.get("stopId"):
                continue
            b = basis_id(s["stopId"])
            if not (b in bekannt or b in ziel_basen or any(s.get("name", "").startswith(n) for n in namen)):
                continue
            stueck = dict(leg, **{"from": einstieg}, to=s, intermediateStops=halte[start + 1:i],
                          startTime=einstieg.get("departure") or einstieg.get("scheduledDeparture"),
                          scheduledStartTime=einstieg.get("scheduledDeparture"),
                          endTime=s.get("arrival") or s.get("scheduledArrival"),
                          scheduledEndTime=s.get("scheduledArrival"))
            zug = Abschnitt.aus_motis(stueck)
            erster = ([Teil(vorne)] if vorne else []) + [Teil([zug])]
            if b in ziel_basen:          # dein Zug fährt direkt bis zum Ziel
                kandidaten.append(Verbindung(erster, v.richtung, v.campus, v.ziel_zeit, neu_geplant=True))
                continue
            mindest = self.regeln.umstieg_darmstadt_min if bewertung.ist_darmstadt_hbf(zug.nach, self.regeln) \
                else self.regeln.umstieg_min
            for z in ziele:
                for t in self._suche(s["stopId"], z, abfahrt=zug.an + timedelta(minutes=mindest), anzahl=2):
                    kandidaten.append(Verbindung(erster + [t], v.richtung, v.campus, v.ziel_zeit,
                                                 neu_geplant=True))
        log.info("Nehme-ich über Fahrt %s: %d Kandidaten", trip, len(kandidaten))
        return kandidaten

    # ------------------------------------------------------------ Neuplanung
    def neu_planen(self, v: Verbindung, jetzt: datetime) -> Optional[Verbindung]:
        """Sucht ab dem Punkt, an dem du laut Fahrplan gerade bist, eine neue Verbindung.

        - Fällt ein Zug aus, den du noch nicht genommen hast: ab dessen Abfahrtshalt.
        - Reicht ein Umstieg nicht mehr: ab dem Umstiegshalt, zur (verspäteten) Ankunft.
        Bereits gefahrene Abschnitte bleiben für die Anzeige vorne dran.
        """
        fahrten = v.fahrten
        start: Optional[tuple[Halt, datetime]] = None
        bis_index = 0   # so viele Fahrten bleiben vorne erhalten
        for k, a in enumerate(fahrten):
            if a.ausfall and a.ab_plan > jetzt - timedelta(minutes=2):
                start, bis_index = (a.von, max(jetzt, a.ab_plan)), k
                break
        if start is None:
            for u in bewertung.umstiege(v, self.regeln):
                if not u.reicht and fahrten[u.nach_index + 1].ab > jetzt:
                    an = fahrten[u.nach_index]
                    start, bis_index = (an.nach, max(jetzt, an.an)), u.nach_index + 1
                    break
        if start is None:
            return None

        halt, zeit = start
        mindest = self.regeln.umstieg_darmstadt_min if bewertung.ist_darmstadt_hbf(halt, self.regeln) \
            else self.regeln.umstieg_min
        if bis_index > 0:
            zeit = zeit + timedelta(minutes=mindest)
        ziele = self.campus_halte(v.campus) if v.richtung == "hin" else [self.hid("gelnhausen")]
        vorne = fahrten[:bis_index]
        kandidaten = []
        for z in ziele:
            for t in self._suche(halt.id, z, abfahrt=zeit, anzahl=4):
                teile = ([Teil(vorne)] if vorne else []) + [t]
                kandidaten.append(Verbindung(teile, v.richtung, v.campus, v.ziel_zeit, neu_geplant=True))
        erg = self._waehle(
            kandidaten,
            lambda x: bewertung.punkte_rettung(x, self.regeln, v.ziel_zeit),
            lambda x: None,
            pflicht_frankfurt=False,
        )
        if erg.verbindung:
            erg.verbindung.hinweis = f"neu geplant ab {halt.name}"
        return erg.verbindung


def mit_anschluss(kandidaten: list[Verbindung], plan: Verbindung, regeln: Regeln,
                  jetzt: datetime) -> list[Verbindung]:
    """Anderer erster Zug, dann weiter wie geplant.

    Beispiel: Geplant ist RB 51 06:37 bis Frankfurt Hbf, dann RB 82. Der RE 50
    06:20 ist 12 min verspätet, kommt aber vor der RB 51 in Frankfurt an. Dann
    ist "RE 50 bis Frankfurt Hbf, dann RB 82" eine eigene Option. Gerechnet
    wird nur mit vorhandenen Daten (Live-Zeiten aus der DB), ohne neue Suche.
    """
    from .texte import kurzname

    pf = plan.fahrten
    ergebnis = []
    for v in kandidaten:
        a = v.fahrten[0] if v.fahrten else None
        if a is None or a.ausfall or a.ab <= jetzt or a.produkt in ("tram", "bus"):
            continue
        if a.linie == pf[0].linie and a.ab_plan == pf[0].ab_plan:
            continue      # derselbe erste Zug wie im Plan
        for i in range(1, len(pf)):
            if pf[i].von.basis != a.nach.basis:
                continue
            neu = Verbindung([Teil([a]), Teil(pf[i:])], plan.richtung, plan.campus, plan.ziel_zeit,
                             hinweis=f"{a.linie} bis {kurzname(a.nach.name)}, dann weiter wie geplant")
            if not bewertung.pruefe(neu, regeln) and (not neu.ziel_zeit or neu.an <= neu.ziel_zeit):
                ergebnis.append(neu)
            break
    return ergebnis


def _verschiedene_zuege(sortiert: list[Verbindung]) -> list[Verbindung]:
    """Pro Zugkombination nur die beste Verbindung behalten. Welche Tram/welcher
    Bus dazu noch passt, zeigt die Takt-Anzeige; als eigene Option wäre das
    nur fast dasselbe nochmal."""
    gesehen, ergebnis = set(), []
    for v in sortiert:
        zuege = tuple(f"{a.linie}@{a.ab_plan:%H%M}" for a in v.fahrten if a.produkt not in ("tram", "bus"))
        # gleiche Ab- und Ankunft (nur anderer Zwischenzug) zählt auch als dasselbe
        zeiten = (v.ab_plan, v.an_plan)
        if zuege in gesehen or zeiten in gesehen:
            continue
        gesehen.update((zuege, zeiten))
        ergebnis.append(v)
    return ergebnis


def _nummer(linie: str) -> str:
    """'RE 50' -> '50', 'S6' -> '6', 'RE60 (4672)' -> '60' (Zugnummer in Klammern weg)."""
    m = re.search(r"(\d+)\s*$", re.sub(r"\s*\(.*?\)\s*$", "", linie or ""))
    return m.group(1) if m else ""


def _erste_fahrt(t: Teil) -> Optional[Abschnitt]:
    return next((a for a in t.abschnitte if not a.fussweg), None)


def _letzte_fahrt(t: Teil) -> Optional[Abschnitt]:
    return next((a for a in reversed(t.abschnitte) if not a.fussweg), None)


def _kombiniere(erste: list[Teil], zweite: list[Teil], richtung: str, campus: str,
                ziel: Optional[datetime], regeln: Regeln) -> list[Verbindung]:
    """Setzt zwei getrennt gesuchte Stücke zusammen, Umstieg in Darmstadt Hbf.

    Zu jedem ersten Stück wird das zweite gewählt, das den Umstieg (mind. 7 min)
    einhält und den größten Puffer lässt, aber noch passt:
    - Hinweg: späteste Tram/Bus, die noch rechtzeitig ankommt. So darf der Zug
      am meisten Verspätung haben, bevor es eng wird.
    - Rückweg: (hier sind erste = Tram/Bus, zweite = Zug) die späteste Tram, die
      den Zug mit mind. 7 min Umstieg erreicht.
    """
    def umstieg_passt(v: Verbindung) -> bool:
        us = bewertung.umstiege(v, regeln)
        if not all(u.reicht for u in us):
            return False
        # Umstieg an der Nahtstelle (Darmstadt Hbf) nicht unendlich lang
        naht = len([a for a in v.teile[0].abschnitte if not a.fussweg]) - 1
        return all(u.minuten <= regeln.kombi_max for u in us if u.nach_index == naht)

    ergebnis = []
    if richtung == "hin":
        for zug in erste:
            passend = []
            for stadt in zweite:
                v = Verbindung([zug, stadt], richtung, campus, ziel)
                if ziel and v.an_plan > ziel:
                    continue
                if umstieg_passt(v):
                    passend.append(v)
            if passend:
                ergebnis.append(max(passend, key=lambda v: v.teile[1].abschnitte[0].ab_plan))
    else:
        for zug in zweite:
            passend = []
            for stadt in erste:
                v = Verbindung([stadt, zug], richtung, campus)
                if umstieg_passt(v):
                    passend.append(v)
            if passend:
                ergebnis.append(max(passend, key=lambda v: v.ab_plan))
    return ergebnis


def _kurz(v: Verbindung) -> str:
    try:
        return f"{v.ab_plan:%H:%M}-{v.an_plan:%H:%M} " + " > ".join(a.linie for a in v.fahrten)
    except (IndexError, AttributeError):
        return "?"
