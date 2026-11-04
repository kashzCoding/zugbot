"""Filter und Bewertung von Verbindungen.

Hier steckt die eigentliche Entscheidungslogik. Ablauf:

1. pruefe()       wirft alles raus, was gegen eine harte Regel verstößt
                  (Fernverkehr, Babenhausen/Groß-Umstadt, zu kurze Umstiege,
                  nicht über Frankfurt, Ausfall).
2. punkte_hin()   bewertet die übrigen Hinweg-Kandidaten. Höher = besser.
   punkte_rueck() dasselbe für den Rückweg.

Alle Zahlen kommen aus config/einstellungen.yaml, damit du sie ohne
Code-Änderung anpassen kannst.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .modell import Abschnitt, Halt, Verbindung, basis_id


@dataclass
class Regeln:
    erlaubte_produkte: set[str]
    verbotene_halte: list[str]
    pflicht_ueber_ids: set[str]
    pflicht_ueber_namen: list[str]
    darmstadt_hbf_id: Optional[str]
    umstieg_min: int
    umstieg_darmstadt_min: int
    komfort_bis: int
    kombi_max: int
    strafe_umstieg: float
    reserve_gewicht: float

    @classmethod
    def aus_einstellungen(cls, e: dict) -> "Regeln":
        h = e["haltestellen"]
        pflicht = [h[k] for k in e.get("pflicht_ueber", [])]
        return cls(
            erlaubte_produkte=set(e["erlaubte_produkte"]),
            verbotene_halte=[n.lower() for n in e.get("verbotene_halte", [])],
            pflicht_ueber_ids={basis_id(str(p["id"])) for p in pflicht if p.get("id")},
            pflicht_ueber_namen=[_norm(p["name"]) for p in pflicht],
            darmstadt_hbf_id=basis_id(str(h["darmstadt_hbf"]["id"])) if h["darmstadt_hbf"].get("id") else None,
            umstieg_min=e["umstieg"]["mindestens_minuten"],
            umstieg_darmstadt_min=e["umstieg"]["darmstadt_hbf_minuten"],
            komfort_bis=e["umstieg"]["komfort_bis_minuten"],
            kombi_max=e["umstieg"].get("darmstadt_hbf_hoechstens_minuten", 30),
            strafe_umstieg=e["bewertung"]["strafe_pro_umstieg_minuten"],
            reserve_gewicht=e["bewertung"]["reserve_gewicht"],
        )


def _norm(name: str) -> str:
    """'Frankfurt(Main)Hbf' und 'Frankfurt (Main) Hauptbahnhof' vergleichbar machen."""
    n = name.lower().replace("hauptbahnhof", "hbf").replace("südbahnhof", "süd")
    return "".join(ch for ch in n if ch.isalnum())


def ist_darmstadt_hbf(halt: Halt, regeln: Regeln) -> bool:
    # Bahnsteige und Tram-/Bussteige "Darmstadt Hauptbahnhof" haben eigene
    # Steig-IDs, zählen aber alle als Umstieg in Darmstadt Hbf.
    return (regeln.darmstadt_hbf_id is not None and halt.basis == regeln.darmstadt_hbf_id) \
        or _norm(halt.name).startswith("darmstadthbf")


@dataclass
class Umstieg:
    ort: str
    minuten: int          # verfügbare Zeit (mit Echtzeit, falls vorhanden)
    minuten_plan: int     # laut Fahrplan
    minimum: int          # geforderte Mindestzeit an diesem Ort
    nach_index: int       # Umstieg nach fahrten[nach_index]

    @property
    def reicht(self) -> bool:
        return self.minuten >= self.minimum

    @property
    def verpasst(self) -> bool:
        return self.minuten < 0


def _minuten(a: datetime, b: datetime) -> int:
    return int((b - a).total_seconds() // 60)


def umstiege(v: Verbindung, regeln: Regeln) -> list[Umstieg]:
    """Umstiege zwischen aufeinanderfolgenden Fahrzeugen.

    Fußwege dazwischen zählen zur Umstiegszeit (du läufst ja in der Zeit).
    """
    fahrten = v.fahrten
    ergebnis = []
    for i in range(len(fahrten) - 1):
        an, ab = fahrten[i], fahrten[i + 1]
        darmstadt = ist_darmstadt_hbf(an.nach, regeln) or ist_darmstadt_hbf(ab.von, regeln)
        ergebnis.append(Umstieg(
            ort=an.nach.name,
            minuten=_minuten(an.an, ab.ab),
            minuten_plan=_minuten(an.an_plan, ab.ab_plan),
            minimum=regeln.umstieg_darmstadt_min if darmstadt else regeln.umstieg_min,
            nach_index=i,
        ))
    return ergebnis


def _gefahrene_halte(a: Abschnitt) -> list[Halt]:
    return [a.von, *a.zwischenhalte, a.nach]


def pruefe(v: Verbindung, regeln: Regeln, pflicht_frankfurt: bool = True) -> list[str]:
    """Gibt Gründe zurück, warum die Verbindung nicht geht. Leer = in Ordnung."""
    gruende = []
    fahrten = v.fahrten
    if not fahrten:
        return ["keine Fahrt enthalten"]

    for a in fahrten:
        # Deutschlandticket: kein ICE/IC/EC
        if a.produkt not in regeln.erlaubte_produkte:
            gruende.append(f"{a.linie or a.produkt}: nicht im Deutschlandticket")
        if a.ausfall:
            gruende.append(f"{a.linie}: fällt aus")
        # Babenhausen/Groß-Umstadt: nur die Halte prüfen, die du wirklich
        # durchfährst. Die RB zur Lichtwiese fährt danach weiter Richtung
        # Groß-Umstadt; das ist egal, solange du vorher aussteigst.
        for h in _gefahrene_halte(a):
            name = h.name.lower()
            if any(verb in name for verb in regeln.verbotene_halte):
                gruende.append(f"{a.linie}: hält in {h.name}")
                break

    if pflicht_frankfurt:
        ueber_frankfurt = any(
            h.basis in regeln.pflicht_ueber_ids or _norm(h.name) in regeln.pflicht_ueber_namen
            for a in fahrten for h in _gefahrene_halte(a)
        )
        if not ueber_frankfurt:
            gruende.append("führt nicht über Frankfurt Hbf oder Frankfurt Süd")

    for u in umstiege(v, regeln):
        if not u.reicht:
            gruende.append(f"Umstieg {u.ort} nur {u.minuten} min (mind. {u.minimum})")
    return gruende


def _reserve_bonus(v: Verbindung, regeln: Regeln) -> float:
    """Bonus für Puffer beim Umsteigen.

    Pro Umstieg zählt die Zeit über dem Minimum, höchstens bis komfort_bis.
    Beispiel: Minimum 5, Umstieg 9 min -> 4 Minuten Reserve.
    Der knappste Umstieg zählt, denn der entscheidet, ob es klappt.
    """
    us = umstiege(v, regeln)
    if not us:
        return regeln.komfort_bis * regeln.reserve_gewicht
    knappste = min(min(u.minuten, regeln.komfort_bis) - u.minimum for u in us)
    return max(0, knappste) * regeln.reserve_gewicht


def _minuten_absolut(t: datetime) -> float:
    # Echter Zeitpunkt in Minuten. Nicht "Minuten seit Mitternacht", sonst
    # sähe ein Zug um 23:59 am Vorabend wie eine sehr späte Abfahrt aus.
    return t.timestamp() / 60


def punkte_hin(v: Verbindung, regeln: Regeln) -> float:
    """Hinweg: so spät wie möglich los, aber zuverlässig.

    Grundwert = Abfahrtszeit (später = mehr Punkte, 1 Punkt je Minute).
    Abzug     = strafe_umstieg je Umstieg (weniger Umsteigen = entspannter).
    Bonus     = Reserve beim knappsten Umstieg (siehe _reserve_bonus).

    Beispiel mit Standardwerten, relativ zu 06:00:
    06:54 mit 2 Umstiegen, knappster Umstieg 2 min über Minimum
      = 54 - 2*4 + 2*0,3 = 46,6 Punkte
    06:41 mit 1 Umstieg, 5 min über Minimum
      = 41 - 1*4 + 5*0,3 = 38,5 Punkte
    Gewinner: 06:54. Soll weniger Umsteigen stärker zählen, erhöhe
    strafe_pro_umstieg_minuten in der einstellungen.yaml.
    """
    return _minuten_absolut(v.ab) - regeln.strafe_umstieg * v.umstiege + _reserve_bonus(v, regeln)


def punkte_rueck(v: Verbindung, regeln: Regeln) -> float:
    """Rückweg: möglichst früh zu Hause.

    Grundwert = minus Ankunftszeit in Gelnhausen (früher = mehr Punkte).
    Abzug/Bonus wie beim Hinweg.
    """
    return -_minuten_absolut(v.an) - regeln.strafe_umstieg * v.umstiege + _reserve_bonus(v, regeln)


def punkte_rettung(v: Verbindung, regeln: Regeln, ziel_zeit: Optional[datetime]) -> float:
    """Neuplanung unterwegs: pünktlich ankommen schlägt alles, sonst früheste Ankunft."""
    puenktlich = ziel_zeit is not None and v.an <= ziel_zeit
    return (10000 if puenktlich else 0) - _minuten_absolut(v.an) - regeln.strafe_umstieg * v.umstiege \
        + _reserve_bonus(v, regeln)
