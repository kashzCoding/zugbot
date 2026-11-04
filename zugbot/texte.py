"""Kurze deutsche Texte für ntfy und Terminal."""

from __future__ import annotations

from typing import Optional

from . import bewertung
from .bewertung import Regeln
from .modell import Abschnitt, Verbindung
from .stundenplan import WOCHENTAGE_LANG, Tag


def kurzname(name: str) -> str:
    """'Frankfurt (Main) Hauptbahnhof' -> 'Frankfurt Hbf', 'Gelnhausen Bahnhof' -> 'Gelnhausen'."""
    n = name.replace(" (Main)", "").replace("Hauptbahnhof", "Hbf").replace("Südbahnhof", "Süd")
    n = n.replace("Darmstadt TU-Lichtwiese", "TU-Lichtwiese")
    return n[:-len(" Bahnhof")] if n.endswith(" Bahnhof") else n


def gleis(a: Abschnitt, an: bool = False) -> str:
    g = a.gleis_an if an else a.gleis_ab
    if not g:
        return ""
    return f" {'Steig' if a.ist_steig else 'Gl.'} {g}"


def dauer(minuten: int) -> str:
    h, m = divmod(minuten, 60)
    return f"{h} h {m} min" if h else f"{m} min"


def campus_name(campus: str) -> str:
    return {"lichtwiese": "Lichtwiese", "stadtmitte": "Stadtmitte"}.get(campus, campus)


def abschnitt_zeile(a: Abschnitt) -> str:
    vs = f" (+{a.verspaetung_ab})" if a.verspaetung_ab > 0 else ""
    return f"{a.ab:%H:%M}{vs} {a.linie} {kurzname(a.von.name)}{gleis(a)} → {kurzname(a.nach.name)} an {a.an:%H:%M}"


def kurzfassung(v: Verbindung, regeln: Regeln) -> str:
    """Mehrzeilig: Abfahrt mit Gleis, Umstiege, Ankunft."""
    f = v.fahrten
    zeilen = [f"Ab {kurzname(f[0].von.name)} {f[0].ab:%H:%M}{gleis(f[0])} ({f[0].linie})"]
    for u, nxt in zip(bewertung.umstiege(v, regeln), f[1:]):
        zeilen.append(f"Umstieg {kurzname(u.ort)} {u.minuten} min → {nxt.linie} {nxt.ab:%H:%M}{gleis(nxt)}")
    zeilen.append(f"An {kurzname(f[-1].nach.name)} {f[-1].an:%H:%M} ({dauer(v.dauer_min)})")
    return "\n".join(zeilen)


def abendplan(tag: Tag, hin: Optional[Verbindung], rueck: Optional[Verbindung], regeln: Regeln) -> tuple[str, str]:
    wt = WOCHENTAGE_LANG[tag.datum.weekday()]
    if tag.frei:
        return f"Morgen frei ({wt})", tag.grund.capitalize() if tag.grund else "Keine Termine."
    titel = f"Morgen {wt}: {campus_name(tag.campus)}"
    teile = []
    if hin:
        teile.append(kurzfassung(hin, regeln))
    else:
        teile.append("Keine passende Hinfahrt gefunden, bitte selbst schauen.")
    t = tag.erster
    teile.append(f"Erster Termin {t.von:%H:%M} {t.titel}, {t.raum}")
    if rueck:
        teile.append(f"Rückfahrt {rueck.ab:%H:%M} ab {kurzname(rueck.fahrten[0].von.name)}, zu Hause {rueck.an:%H:%M}")
    teile += tag.warnungen
    return titel, "\n".join(teile)


def morgenstand(hin: Verbindung, regeln: Regeln) -> tuple[str, str]:
    f = hin.fahrten[0]
    puenktlich = "pünktlich" if f.verspaetung_ab <= 0 else f"+{f.verspaetung_ab} min"
    return f"Los um {f.ab:%H:%M} ({puenktlich})", kurzfassung(hin, regeln)


def terminal(v: Optional[Verbindung], regeln: Regeln) -> str:
    if v is None:
        return "  (keine passende Verbindung)"
    zeilen = []
    fahrten = v.fahrten
    us = bewertung.umstiege(v, regeln)
    for i, a in enumerate(fahrten):
        # Fußweg direkt vor diesem Fahrzeug anzeigen (z. B. vom Tram- zum Bahnsteig)
        alle = v.abschnitte
        idx = alle.index(a)
        if idx > 0 and alle[idx - 1].fussweg and i == 0:
            w = alle[idx - 1]
            zeilen.append(f"         Fußweg {kurzname(w.von.name)} → {kurzname(w.nach.name)} "
                          f"({w.fahrzeit_min} min)")
        g = gleis(a).strip()
        zeilen.append(f"  {a.ab:%H:%M}  {a.linie:<9} {kurzname(a.von.name)}{(' ' + g) if g else ''} → {kurzname(a.nach.name)}"
                      f"  an {a.an:%H:%M}{gleis(a, an=True)}  ({a.fahrzeit_min} min)")
        if i < len(us):
            zeilen.append(f"         Umstieg {us[i].minuten} min (mind. {us[i].minimum})")
    zeilen.append(f"  Gesamt {dauer(v.dauer_min)}, {v.umstiege} Umstieg(e)")
    return "\n".join(zeilen)
