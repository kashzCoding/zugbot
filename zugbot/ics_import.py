"""Stundenplan aus einem TUCaN-iCal-Export (.ics) übernehmen.

TUCaN exportiert jeden einzelnen Termin als eigenen Eintrag (kein Abo, keine
Wiederholungsregeln). Der Import fasst Einträge mit gleichem Wochentag,
gleicher Uhrzeit und gleichem Titel zu einem wöchentlichen Termin zusammen.
Ändert sich der Raum im Lauf des Semesters, wird das ein "raumwechsel".

Ablauf:
  zugbot import-ics export.ics              zeigt, was erkannt wurde (ändert nichts)
  zugbot import-ics export.ics --schreiben  ersetzt die Termine in stundenplan.yaml
                                             (Semesterdaten und freie Tage bleiben,
                                             alte Datei wird als .bak gesichert)
Danach Termine, die du nicht besuchst, in stundenplan.yaml auf "aktiv: false" setzen.
"""

from __future__ import annotations

import shutil
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import yaml
from icalendar import Calendar

from . import config
from .stundenplan import WOCHENTAGE, campus_aus_raum


def _lokal(dt) -> datetime:
    if isinstance(dt, datetime):
        return dt.astimezone(config.zeitzone()) if dt.tzinfo else dt.replace(tzinfo=config.zeitzone())
    raise ValueError("ganztägiger Termin")


def _raum(location: str) -> str:
    # TUCaN schreibt Räume teils als "S1|05/122" oder mit Gebäudename in Klammern
    r = location.split("(")[0].strip()
    return r.replace("/", " ", 1) if "|" in r else r


def termine_lesen(pfad: Path) -> list[dict]:
    kal = Calendar.from_ical(pfad.read_bytes())
    gruppen: dict[tuple, list[tuple[date, str]]] = defaultdict(list)
    for ev in kal.walk("VEVENT"):
        try:
            von, bis = _lokal(ev.decoded("DTSTART")), _lokal(ev.decoded("DTEND"))
        except (KeyError, ValueError):
            continue
        titel = str(ev.get("SUMMARY", "")).strip()
        raum = _raum(str(ev.get("LOCATION", "")))
        schluessel = (von.weekday(), von.strftime("%H:%M"), bis.strftime("%H:%M"), titel)
        gruppen[schluessel].append((von.date(), raum))

    termine = []
    for (wt, v, b, titel), vorkommen in sorted(gruppen.items()):
        if wt > 4:
            continue  # Wochenende ignorieren
        vorkommen.sort()
        eintrag = {"tag": WOCHENTAGE[wt], "von": v, "bis": b, "raum": vorkommen[0][1], "titel": titel}
        wechsel, aktuell = [], vorkommen[0][1]
        for d, raum in vorkommen[1:]:
            if raum != aktuell:
                wechsel.append({"ab": d, "raum": raum})
                aktuell = raum
        if wechsel:
            eintrag["raumwechsel"] = wechsel
        if len(vorkommen) <= 2:
            # nur ein- oder zweimal: vermutlich Einzeltermin, Zeitraum eingrenzen
            eintrag["ab"], eintrag["bis_datum"] = vorkommen[0][0], vorkommen[-1][0]
        if campus_aus_raum(eintrag["raum"]) is None:
            eintrag["campus"] = "lichtwiese  # BITTE PRÜFEN, Raum unklar"
        eintrag["aktiv"] = True
        termine.append(eintrag)
    return termine


def importieren(pfad: Path, schreiben: bool) -> int:
    termine = termine_lesen(pfad)
    if not termine:
        print("Keine Termine in der Datei gefunden.")
        return 1
    print(f"{len(termine)} wöchentliche Termine erkannt:\n")
    for t in termine:
        extra = f"  (Raumwechsel: {', '.join(str(w['ab']) + ' ' + w['raum'] for w in t['raumwechsel'])})" \
            if "raumwechsel" in t else ""
        print(f"  {t['tag']} {t['von']}-{t['bis']}  {t['raum']:<14} {t['titel']}{extra}")
    if not schreiben:
        print("\nNichts geändert. Mit --schreiben übernehmen.")
        return 0

    ziel = config.CONFIG_DIR / "stundenplan.yaml"
    if ziel.exists():
        shutil.copy(ziel, ziel.with_suffix(".yaml.bak"))
    alt = config.stundenplan_roh()
    neu = {"semester": alt.get("semester", {}), "vorlesungsfrei": alt.get("vorlesungsfrei", []),
           "termine": termine}
    kopf = ("# Aus TUCaN importiert am {:%d.%m.%Y %H:%M}. Vorherige Fassung: stundenplan.yaml.bak\n"
            "# Termine, die du nicht besuchst: aktiv: false setzen.\n"
            "# Aufbau und Felder siehe README.md.\n\n").format(datetime.now())
    text = yaml.safe_dump(neu, allow_unicode=True, sort_keys=False, default_flow_style=False)
    # "lichtwiese  # BITTE PRÜFEN" soll als Kommentar in der Datei landen, nicht als Text
    text = text.replace("campus: 'lichtwiese  # BITTE PRÜFEN, Raum unklar'",
                        "campus: lichtwiese  # BITTE PRÜFEN, Raum unklar")
    ziel.write_text(kopf + text, encoding="utf-8")
    print(f"\n{ziel} geschrieben, alte Fassung als {ziel.name}.bak gesichert.")
    print("Danach: sudo systemctl restart zugbot")
    return 0
