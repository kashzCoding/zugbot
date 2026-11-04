"""Live-Stand holen und entscheiden, was für dich relevant ist.

aktualisiere() holt Verspätungen, Gleise und Ausfälle für eine gespeicherte
Verbindung. lage() vergleicht den neuen Stand mit dem, was dir zuletzt
gemeldet wurde, und liefert nur Meldungen mit Folgen:

  ausfall           ein Zug/Bus fällt aus                    -> neu planen
  verpasst          Anschluss nicht mehr erreichbar          -> neu planen
  umstieg_knapp     Umstieg unter Mindestzeit                -> neu planen versuchen
  gleis             Gleis/Steig geändert (noch nicht abgefahren)
  zu_spaet          Hinweg kommt nach Veranstaltungsbeginn an
  abfahrt_spaeter   erste Abfahrt deutlich verspätet (du kannst später los)

Kleine Verspätungen ohne Folgen erzeugen keine Meldung.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from . import bewertung
from .bahn import Bahn
from .bewertung import Regeln
from .modell import Teil, Verbindung
from .texte import kurzname

log = logging.getLogger(__name__)


def aktualisiere(bahn: Bahn, v: Verbindung, gleise=None) -> Verbindung:
    """Holt den aktuellen Stand jedes Teils über dessen refreshToken.

    Teile ohne Token (z. B. bereits gefahrene Abschnitte nach einer
    Neuplanung) bleiben unverändert. Ein abgelaufener Token (HTTP 4xx)
    lässt den alten Stand stehen; BahnFehler (Schnittstelle weg) geht
    nach oben, damit der Dienst den Ausfall zählen kann.
    """
    neue_teile = []
    for t in v.teile:
        if not t.refresh_token:
            neue_teile.append(t)
            continue
        try:
            itinerary = bahn.aktualisieren(t.refresh_token)
            frisch = Teil.aus_motis(itinerary)
            frisch.refresh_token = itinerary.get("id") or t.refresh_token
            neue_teile.append(frisch if frisch.abschnitte else t)
        except ValueError as exc:
            log.warning("Aktualisieren fehlgeschlagen, behalte alten Stand: %s", exc)
            neue_teile.append(t)
    neu = Verbindung(neue_teile, v.richtung, v.campus, v.ziel_zeit, v.neu_geplant, v.hinweis)
    if gleise is not None:
        gleise.ergaenzen(neu)
    return neu


@dataclass
class Meldung:
    art: str
    text: str
    dringend: bool = False     # ntfy-Priorität hoch
    neu_planen: bool = False


def zustand_schluessel(v: Verbindung, regeln: Regeln, jetzt: datetime) -> dict[str, str]:
    """Kompakte Beschreibung des relevanten Zustands.

    Gemeldet wird nur, wenn sich ein Eintrag hier gegenüber der letzten
    Meldung ändert. So kommt dieselbe Nachricht nicht alle 5 Minuten.
    """
    z: dict[str, str] = {}
    fahrten = v.fahrten
    for i, a in enumerate(fahrten):
        if a.an < jetzt:
            continue   # schon vorbei, interessiert nicht mehr
        if a.ausfall:
            z[f"ausfall:{i}"] = a.linie
        if a.ab > jetzt and a.gleis_ab and a.gleis_ab_plan and a.gleis_ab != a.gleis_ab_plan:
            z[f"gleis:{i}"] = a.gleis_ab
    for u in bewertung.umstiege(v, regeln):
        if fahrten[u.nach_index + 1].ab < jetzt:
            continue
        if u.verpasst:
            z[f"umstieg:{u.nach_index}"] = "verpasst"
        elif not u.reicht:
            z[f"umstieg:{u.nach_index}"] = "knapp"
    if v.richtung == "hin" and v.ziel_zeit and v.an > v.ziel_zeit:
        z["zu_spaet"] = v.an.strftime("%H:%M")
    erste = fahrten[0] if fahrten else None
    if erste and erste.ab > jetzt and erste.verspaetung_ab >= 1:
        # in 5-Minuten-Stufen, damit +6 -> +7 keine neue Meldung auslöst
        z["abfahrt"] = str(erste.verspaetung_ab // 5 * 5)
    return z


def lage(v: Verbindung, regeln: Regeln, jetzt: datetime, zuletzt: dict[str, str],
         abfahrt_schwelle: int) -> list[Meldung]:
    """Erzeugt Meldungen für alles, was sich seit 'zuletzt' relevant geändert hat."""
    jetzt_z = zustand_schluessel(v, regeln, jetzt)
    fahrten = v.fahrten
    meldungen: list[Meldung] = []

    for key, wert in jetzt_z.items():
        if zuletzt.get(key) == wert:
            continue
        art, _, idx = key.partition(":")
        if art == "ausfall":
            a = fahrten[int(idx)]
            meldungen.append(Meldung("ausfall", f"{a.linie} {a.ab_plan:%H:%M} ab {kurzname(a.von.name)} fällt aus.",
                                     dringend=True, neu_planen=True))
        elif art == "gleis":
            a = fahrten[int(idx)]
            wort = "Steig" if a.ist_steig else "Gleis"
            meldungen.append(Meldung("gleis", f"{wort}wechsel: {a.linie} {a.ab:%H:%M} ab {kurzname(a.von.name)} "
                                              f"jetzt {wort} {a.gleis_ab} (statt {a.gleis_ab_plan})."))
        elif art == "umstieg":
            u = next(x for x in bewertung.umstiege(v, regeln) if x.nach_index == int(idx))
            an, ab = fahrten[u.nach_index], fahrten[u.nach_index + 1]
            if wert == "verpasst":
                text = (f"Anschluss weg: {an.linie} kommt {an.an:%H:%M} in {kurzname(u.ort)} an, "
                        f"{ab.linie} fährt {ab.ab:%H:%M}.")
            else:
                text = (f"Umstieg {kurzname(u.ort)} knapp: {u.minuten} min statt {u.minuten_plan} "
                        f"({an.linie} +{an.verspaetung_an}).")
            meldungen.append(Meldung("umstieg", text, dringend=(wert == "verpasst"), neu_planen=True))
        elif art == "zu_spaet":
            meldungen.append(Meldung("zu_spaet", f"Ankunft jetzt {wert}, geplant war spätestens "
                                                 f"{v.ziel_zeit:%H:%M}.", dringend=True, neu_planen=True))
        elif art == "abfahrt" and int(wert) >= abfahrt_schwelle:
            a = fahrten[0]
            meldungen.append(Meldung("abfahrt", f"{a.linie} ab {kurzname(a.von.name)} heute {a.ab:%H:%M} "
                                                f"(+{a.verspaetung_ab}). Du kannst später los."))

    # Entwarnung, wenn ein gemeldetes Problem verschwunden ist
    for key in zuletzt:
        if key.startswith(("umstieg:", "zu_spaet")) and key not in jetzt_z:
            meldungen.append(Meldung("entwarnung", "Entwarnung: Verbindung klappt wieder wie geplant."))
            break
    return meldungen


def braucht_neuplanung(meldungen: list[Meldung]) -> bool:
    return any(m.neu_planen for m in meldungen)


def ist_besser(neu: Optional[Verbindung], alt: Verbindung, regeln: Regeln, jetzt: datetime) -> bool:
    """Neue Route nur übernehmen, wenn sie die Lage wirklich verbessert."""
    if neu is None or neu.schluessel() == alt.schluessel():
        return False
    alt_kaputt = any(a.ausfall for a in alt.fahrten if a.an >= jetzt) or any(
        not u.reicht for u in bewertung.umstiege(alt, regeln) if alt.fahrten[u.nach_index + 1].ab >= jetzt)
    if alt_kaputt:
        return True
    return neu.an < alt.an
