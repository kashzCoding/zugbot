"""Abfahrtstafel am Bahnhof deines nächsten Zuges, nur Züge in deine Richtung.

Der Dienst baut sie ab 90 min vor der Abfahrt bei jeder Prüfung. Das Dashboard
kann sie auch vorher auf Wunsch laden ("Andere Züge anzeigen", immer=True).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from . import bewertung, texte
from .bewertung import Regeln
from .modell import Verbindung

AB_MINUTEN = 90     # ohne Wunsch erst so kurz vor dem nächsten Zug


def bauen(v: Verbindung, jetzt: datetime, gleise, regeln: Regeln, e: dict, immer: bool = False) -> Optional[dict]:
    zuege = [a for a in v.fahrten if a.produkt not in ("tram", "bus")]
    naechster = next((a for a in zuege if a.ab > jetzt - timedelta(minutes=1)), None)
    if naechster is None or (not immer and jetzt < naechster.ab_plan - timedelta(minutes=AB_MINUTEN)):
        return None
    eva = gleise.eva.get(naechster.von.basis)
    if not eva or not gleise.aktiv:
        return None
    i = zuege.index(naechster)
    rest = zuege[i:]
    # Am Umstiegsbahnhof erst ab deiner Ankunft dort (mit Umstiegszeit) zählen
    # Auch Züge, die knapp nach deiner Ankunft fahren: Die erreicht man oft noch.
    frueh = jetzt - timedelta(minutes=2)
    knapp_bis = None
    if i > 0:
        minimum = regeln.umstieg_darmstadt_min if bewertung.ist_darmstadt_hbf(naechster.von, regeln) \
            else regeln.umstieg_min
        frueh = max(frueh, zuege[i - 1].an)
        knapp_bis = zuege[i - 1].an + timedelta(minutes=minimum)
    if immer:       # Stunden vorher: die Züge rund um deine Abfahrt, nicht die von jetzt
        frueh = max(frueh, naechster.ab - timedelta(minutes=45))
    ziele = [a.nach.name for a in rest]
    # Frankfurt Hbf und Süd sind gleichwertig (Pflicht: über einen der beiden)
    if any(bewertung.ist_pflicht_halt(a.nach, regeln) for a in rest):
        ziele += [e["haltestellen"][k]["name"] for k in e["pflicht_ueber"]]
    zuege_tafel = gleise.tafel(eva, frueh, max(naechster.ab, frueh) + timedelta(minutes=40), ziele)
    # "dein Zug" markiert erst das Dashboard (web.ist_deiner), aus dem aktuellen
    # Plan: nach "Nehme ich" ist die gemerkte Tafel sonst veraltet.
    for zt in zuege_tafel:
        ist = datetime.strptime(zt["pt"], "%y%m%d%H%M").replace(tzinfo=jetzt.tzinfo) + \
            timedelta(minutes=zt.get("plus") or 0)
        zt["knapp"] = bool(knapp_bis and ist < knapp_bis)
    return {"bahnhof": texte.kurzname(naechster.von.name), "halt_id": naechster.von.id,
            "stand": jetzt.isoformat(), "zuege": zuege_tafel[:8]}
