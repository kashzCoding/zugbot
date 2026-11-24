"""Letzte Meile: welche Trams und Busse zwischen Darmstadt Hbf und Campus passen.

Der Planer speichert beim Planen alle direkten Tram-/Busfahrten im passenden
Zeitfenster (planer.stadtfahrten). Hier wird daraus pro Verbindung berechnet,
welche davon noch passen, mit Echtzeit des Zuges:

- Hinweg:  ab Darmstadt Hbf frühestens Zugankunft + Umstieg (7 min),
           am Campus spätestens zur Zielzeit
- Rückweg: ab Campus frühestens zur Rückfahrtzeit,
           am Darmstadt Hbf spätestens Zugabfahrt - Umstieg
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from . import bewertung
from .bewertung import Regeln
from .modell import Verbindung, _zeit

ANZEIGEN_HOECHSTENS = 6
LIVE_HOECHSTENS = 10    # nachgeladen ("Weitere Trams")


def takt_text(abfahrten: list[datetime]) -> str:
    """'etwa alle 5 min' bzw. 'etwa alle 5–10 min' aus den typischen Abständen."""
    zeiten = sorted(set(abfahrten))
    abstaende = sorted(round((b - a).total_seconds() / 60) for a, b in zip(zeiten, zeiten[1:]))
    abstaende = [x for x in abstaende if x > 0]
    if not abstaende:
        return ""
    # mittlere Hälfte, damit eine einzelne Lücke den Takt nicht verfälscht
    unten = abstaende[len(abstaende) // 4]
    oben = abstaende[(3 * len(abstaende)) // 4] if len(abstaende) > 1 else unten
    if oben - unten <= 2:
        return f"etwa alle {round((unten + oben) / 2)} min"
    return f"etwa alle {unten}–{oben} min"


def _linien(fahrten: list[dict]) -> str:
    """'Tram 2, 3, Bus H' – häufigste Linien zuerst, Verkehrsmittel zusammengefasst."""
    zaehler: dict[str, int] = {}
    for f in fahrten:
        zaehler[f["linie"]] = zaehler.get(f["linie"], 0) + 1
    gruppen: dict[str, list[str]] = {}
    for linie in sorted(zaehler, key=lambda x: -zaehler[x]):
        art, _, nr = linie.partition(" ")
        gruppen.setdefault(art, []).append(nr or art)
    return ", ".join(f"{art} {', '.join(nrn)}" for art, nrn in gruppen.items())


def info(stadt: Optional[dict], v: Verbindung, regeln: Regeln,
         live: Optional[list[dict]] = None) -> Optional[dict]:
    """Passende Trams/Busse für diese Verbindung, oder None (z. B. ohne Darmstadt Hbf).

    Jede Fahrt kommt mit "knapp" (weniger Umstiegszeit als die Regel) und
    "zu_spaet" (hin: nach der Zielzeit am Campus, zurück: Zug schon weg), damit
    du auch bei Verspätung eine Tram auswählen kannst. live = im Dashboard
    nachgeladene Fahrten ("Weitere Trams"): dann alle davon, nicht nur die passenden.
    """
    quelle = live if live else (stadt or {}).get("fahrten")
    if not quelle:
        return None
    fahrten = [dict(f, ab_dt=_zeit(f["ab"]), an_dt=_zeit(f["an"])) for f in quelle]
    umstieg = timedelta(minutes=regeln.umstieg_darmstadt_min)
    if v.richtung == "hin":
        zug = next((a for a in v.fahrten if a.produkt not in ("tram", "bus")
                    and bewertung.ist_darmstadt_hbf(a.nach, regeln)), None)
        if zug is None:
            return None
        for f in fahrten:
            f["knapp"] = f["ab_dt"] < zug.an + umstieg
            f["zu_spaet"] = bool(v.ziel_zeit and f["an_dt"] > v.ziel_zeit)
        passend = [f for f in fahrten if f["ab_dt"] >= zug.an]
        if not live:
            # Erst die, mit denen du rechtzeitig ankommst; bist du ohnehin zu
            # spät, die nächsten danach (sonst stünde gar keine Tram da).
            rechtzeitig = [f for f in passend if not f["zu_spaet"]]
            passend = rechtzeitig or passend
        titel = "Ab Darmstadt Hbf zum Campus"
        halt = v.fahrten.index(zug) + 1      # Halt nach der Zugankunft
    else:
        zug = next((a for a in v.fahrten if a.produkt not in ("tram", "bus")
                    and bewertung.ist_darmstadt_hbf(a.von, regeln)), None)
        if zug is None:
            return None
        for f in fahrten:
            f["knapp"] = f["an_dt"] > zug.ab - umstieg
            f["zu_spaet"] = f["an_dt"] > zug.ab
        frueh = _zeit((stadt or {}).get("ab"))
        passend = fahrten if live else \
            [f for f in fahrten if not f["zu_spaet"] and (frueh is None or f["ab_dt"] >= frueh)]
        titel = "Zum Darmstadt Hbf"
        halt = max(0, v.fahrten.index(zug) - 1)   # Abfahrtshalt der Tram/des Busses am Campus
    if not passend:
        return None
    eigene = {(a.linie, a.ab_plan) for a in v.fahrten if a.produkt in ("tram", "bus")}
    passend = sorted(passend, key=lambda f: f["ab_dt"])[:LIVE_HOECHSTENS] if live \
        else _auswahl(passend, eigene, v.richtung)
    linien = _linien(passend)
    nur = {f["linie"] for f in passend}
    if len(nur) == 1:    # eine Linie: deren eigener Takt
        takt = takt_text([f["ab_dt"] for f in fahrten if f["linie"] in nur])
        text = f"{linien} {takt}".strip()
    else:                # mehrere: wie oft überhaupt etwas fährt
        takt = takt_text([f["ab_dt"] for f in fahrten])
        takt = f"zusammen {takt}" if takt else ""
        text = f"{linien} · {takt}" if takt else linien
    return {
        "titel": titel,
        "halt": halt,       # Index im Zeitstrahl (web.fahrt_daten "halte")
        "linien": linien,
        "takt": takt,
        "text": text,
        "live": bool(live),
        "fahrten": [{
            "ab": f"{f['ab_dt']:%H:%M}", "an": f"{f['an_dt']:%H:%M}", "linie": f["linie"],
            "nach": f.get("nach", ""), "steig": f.get("steig") or "",
            "deine": (f["linie"], f["ab_dt"]) in eigene,
            "knapp": f["knapp"], "zu_spaet": f["zu_spaet"],
            "pt": f"{f['ab_dt']:%y%m%d%H%M}", "ab_dt": f["ab_dt"].isoformat(),
            "halt_id": f.get("von_id") or "",
        } for f in passend],
    }


def _auswahl(passend: list[dict], eigene: set, richtung: str) -> list[dict]:
    """Höchstens ANZEIGEN_HOECHSTENS Fahrten: hin die ersten nach der Zugankunft,
    zurück die letzten vor der Zugabfahrt. Die eigene Fahrt ist immer dabei."""
    n = ANZEIGEN_HOECHSTENS
    auswahl = passend[:n] if richtung == "hin" else passend[-n:]
    eigen = [f for f in passend if (f["linie"], f["ab_dt"]) in eigene and f not in auswahl]
    if eigen:
        auswahl = (auswahl[:n - len(eigen)] if richtung == "hin" else auswahl[len(eigen):]) + eigen
    return sorted(auswahl, key=lambda f: f["ab_dt"])
