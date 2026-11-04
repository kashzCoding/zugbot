"""Stundenplan: welche Termine an welchem Tag, welcher Campus, freie Tage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Optional

from . import config

WOCHENTAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
WOCHENTAGE_LANG = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


@dataclass
class Termin:
    titel: str
    raum: str
    von: datetime
    bis: datetime
    campus: str


@dataclass
class Tag:
    datum: date
    frei: bool
    grund: str = ""
    termine: list[Termin] = field(default_factory=list)
    campus: Optional[str] = None
    warnungen: list[str] = field(default_factory=list)

    @property
    def erster(self) -> Optional[Termin]:
        return self.termine[0] if self.termine else None

    @property
    def letzter(self) -> Optional[Termin]:
        return max(self.termine, key=lambda t: t.bis) if self.termine else None


def _datum(wert) -> date:
    if isinstance(wert, date):
        return wert
    return date.fromisoformat(str(wert))


def _uhrzeit(wert) -> time:
    if isinstance(wert, int):  # YAML liest 08:00 ohne Anführungszeichen manchmal als Minuten
        return time(wert // 60, wert % 60)
    h, m = str(wert).split(":")
    return time(int(h), int(m))


def campus_aus_raum(raum: str) -> Optional[str]:
    """L... -> Lichtwiese, S... -> Stadtmitte (Gebäudekennung der TU Darmstadt)."""
    r = raum.strip().upper()
    if r.startswith("L"):
        return "lichtwiese"
    if r.startswith("S"):
        return "stadtmitte"
    return None


def _freier_grund(d: date, plan: dict) -> Optional[str]:
    sem = plan.get("semester", {})
    if "vorlesungen_ab" in sem and d < _datum(sem["vorlesungen_ab"]):
        return "vor Vorlesungsbeginn"
    if "vorlesungen_bis" in sem and d > _datum(sem["vorlesungen_bis"]):
        return "nach Vorlesungsende"
    for eintrag in plan.get("vorlesungsfrei", []) or []:
        if "datum" in eintrag and _datum(eintrag["datum"]) == d:
            return eintrag.get("grund", "vorlesungsfrei")
        if "von" in eintrag and _datum(eintrag["von"]) <= d <= _datum(eintrag["bis"]):
            return eintrag.get("grund", "vorlesungsfrei")
    return None


def tag(d: date, plan: Optional[dict] = None) -> Tag:
    plan = plan if plan is not None else config.stundenplan_roh()
    tz = config.zeitzone()

    grund = _freier_grund(d, plan)
    if grund:
        return Tag(d, frei=True, grund=grund)

    kuerzel = WOCHENTAGE[d.weekday()]
    termine: list[Termin] = []
    warnungen: list[str] = []
    for t in plan.get("termine", []) or []:
        if t.get("tag") != kuerzel or t.get("aktiv", True) is False:
            continue
        if "ab" in t and d < _datum(t["ab"]):
            continue
        if "bis_datum" in t and d > _datum(t["bis_datum"]):
            continue
        raum = str(t.get("raum", ""))
        for wechsel in sorted(t.get("raumwechsel", []) or [], key=lambda w: _datum(w["ab"])):
            if d >= _datum(wechsel["ab"]):
                raum = str(wechsel["raum"])
        campus = t.get("campus") or campus_aus_raum(raum)
        if campus is None:
            warnungen.append(f"Campus für „{t.get('titel')}“ ({raum}) unklar, bitte campus: setzen.")
            continue
        termine.append(Termin(
            titel=str(t.get("titel", "")),
            raum=raum,
            von=datetime.combine(d, _uhrzeit(t["von"]), tz),
            bis=datetime.combine(d, _uhrzeit(t["bis"]), tz),
            campus=campus,
        ))

    termine.sort(key=lambda x: x.von)
    if not termine:
        return Tag(d, frei=True, grund="keine Termine", warnungen=warnungen)

    campi = {t.campus for t in termine}
    if len(campi) > 1:
        warnungen.append("Achtung: an diesem Tag stehen Termine auf beiden Campus. "
                         "Geplant wird zum Campus des ersten Termins.")
    return Tag(d, frei=False, termine=termine, campus=termine[0].campus, warnungen=warnungen)
