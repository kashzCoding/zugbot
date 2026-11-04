"""Verbindungssuche für Hin- und Rückweg und Neuplanung unterwegs.

Transitous kann "nur Nahverkehr" und "über X", aber nicht
"nie über Babenhausen" oder "in Darmstadt Hbf mind. 7 min". Deshalb stellt
der Planer mehrere Anfragen und filtert/bewertet selbst (bewertung.py).

Lichtwiese:  eine Suche Gelnhausen -> Lichtwiese, je über Frankfurt Hbf und Süd,
             je zum Bahnhof und zum Tram-Halt Campus (4 Anfragen).
Stadtmitte:  zweistufig, weil der Umstieg in Darmstadt Hbf (mind. 7 min)
             zwischen zwei getrennt gesuchten Stücken liegt:
             Zug Gelnhausen <-> Darmstadt Hbf und Tram/Bus Darmstadt Hbf <-> Schloss/WBP.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from . import bewertung
from .bahn import Bahn, BahnFehler
from .bewertung import Regeln
from .modell import Abschnitt, Halt, Teil, Verbindung, basis_id
from .stundenplan import Tag

log = logging.getLogger(__name__)

NAHVERKEHR_STADT = ["BUS", "TRAM"]   # für das Stück Hbf <-> Schloss/WBP


@dataclass
class Ergebnis:
    verbindung: Optional[Verbindung]
    kandidaten: int = 0
    verworfen: list[str] = field(default_factory=list)   # Gründe, für "zugbot plan --details"


class Planer:
    def __init__(self, bahn: Bahn, einstellungen: dict):
        from .gleise import DbGleise

        self.bahn = bahn
        self.e = einstellungen
        self.regeln = Regeln.aus_einstellungen(einstellungen)
        self.gleise = DbGleise(einstellungen)

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
        beste = gueltig[0] if gueltig else None
        self.gleise.ergaenzen(beste)   # Zuggleise aus der DB-API, falls Schlüssel vorhanden
        return Ergebnis(beste, len(gesehen), verworfen)

    # ------------------------------------------------------------ Hinweg
    def ziel_zeit(self, tag: Tag) -> datetime:
        h = self.e["hinweg"]
        puffer = h["ankunftspuffer_minuten"] + h["gehzeit_minuten"].get(tag.campus, 0)
        return tag.erster.von - timedelta(minutes=puffer)

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
