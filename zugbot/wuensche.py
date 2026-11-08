"""Deine Wünsche für einzelne Tage (daten/wuensche.json):

- eigene Zeiten: Ankunft (zählt wie Vorlesungsbeginn), Rückfahrt frühestens ab, Campus
- gewählte Verbindung: welche der Optionen der Dienst überwachen soll
- genommener Zug: "Ich nehme diesen Zug" auf der Abfahrtstafel

Eigene Datei statt zustand.json: Dashboard und "zugbot anpassen" schreiben hier,
der Dienst liest nur. So überschreibt der Dienst beim Speichern keinen Wunsch,
der während einer Planung hereinkommt. Der Dienst übernimmt Änderungen im
nächsten Takt; GEAENDERT weckt ihn sofort, wenn das Dashboard etwas schreibt.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import date, datetime, timedelta
from typing import Optional

from . import config, stundenplan
from .stundenplan import Tag, Termin

_lock = threading.Lock()
PFAD = config.DATEN_DIR / "wuensche.json"
GEAENDERT = threading.Event()
TAGE_VORAUS = 14     # so weit im Voraus darf man Zeiten eintragen


def _heute() -> date:
    return datetime.now(config.zeitzone()).date()


def laden() -> dict:
    with _lock:
        try:
            return json.loads(PFAD.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}


def _speichern(w: dict) -> None:
    PFAD.parent.mkdir(parents=True, exist_ok=True)
    tmp = PFAD.with_suffix(".tmp")
    tmp.write_text(json.dumps(w, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, PFAD)


def _aendern(funktion) -> None:
    with _lock:
        try:
            w = json.loads(PFAD.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            w = {}
        funktion(w)
        heute = _heute().isoformat()
        for bereich in ("anpassungen", "wahl", "zug"):       # Vergangenes aufräumen
            for k in list(w.get(bereich, {})):
                if k[:10] < heute:
                    del w[bereich][k]
        _speichern(w)
    GEAENDERT.set()


# ------------------------------------------------------------ Prüfen
def datum_lesen(text: str, heute: Optional[date] = None) -> date:
    """'heute', 'morgen' oder JJJJ-MM-TT."""
    heute = heute or _heute()
    t = text.strip().lower()
    if t == "heute":
        return heute
    if t == "morgen":
        return heute + timedelta(days=1)
    try:
        return date.fromisoformat(t)
    except ValueError:
        raise ValueError(f"Datum „{text}“ nicht erkannt, bitte JJJJ-MM-TT, heute oder morgen.")


def _uhrzeit(text: Optional[str], was: str) -> Optional[str]:
    if text is None or str(text).strip() == "":
        return None
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", str(text).strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"{was} „{text}“ ist keine Uhrzeit (HH:MM).")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def pruefen(d: date, an=None, rueck_ab=None, campus=None, heute: Optional[date] = None) -> dict:
    """Gibt die bereinigte Anpassung zurück oder wirft ValueError mit deutschem Text."""
    heute = heute or _heute()
    if not heute <= d <= heute + timedelta(days=TAGE_VORAUS):
        raise ValueError(f"Eigene Zeiten gehen nur für heute bis in {TAGE_VORAUS} Tagen.")
    a = {"an": _uhrzeit(an, "Ankunft"), "rueck_ab": _uhrzeit(rueck_ab, "Rückfahrt")}
    if campus not in (None, ""):
        if campus not in config.einstellungen()["campus"]:
            raise ValueError(f"Campus „{campus}“ gibt es nicht.")
        a["campus"] = campus
    else:
        a["campus"] = None
    if a["an"] and a["rueck_ab"] and a["rueck_ab"] <= a["an"]:
        raise ValueError("Die Rückfahrt muss nach der Ankunft liegen.")
    return a


# ------------------------------------------------------------ Schreiben
def anpassen(d: date, an=None, rueck_ab=None, campus=None) -> Optional[dict]:
    """Setzt eigene Zeiten für einen Tag. Alles leer = zurücksetzen."""
    a = pruefen(d, an, rueck_ab, campus)
    leer = not any(a.values())

    def f(w):
        w.setdefault("anpassungen", {})
        if leer:
            w["anpassungen"].pop(d.isoformat(), None)
        else:
            w["anpassungen"][d.isoformat()] = a
        # Neue Zeiten = neue Optionen, eine alte Wahl passt dann nicht mehr
        for r in ("hin", "rueck"):
            w.get("wahl", {}).pop(f"{d.isoformat()}:{r}", None)

    _aendern(f)
    return None if leer else a


def zuruecksetzen(d: date) -> None:
    anpassen(d)


def waehlen(d: date, richtung: str, schluessel: str) -> None:
    if richtung not in ("hin", "rueck"):
        raise ValueError("Richtung muss hin oder rueck sein.")
    _aendern(lambda w: w.setdefault("wahl", {}).__setitem__(f"{d.isoformat()}:{richtung}", schluessel))


def zug_nehmen(d: date, richtung: str, zug: dict) -> None:
    """zug = {"halt_id", "linie", "ab_plan" (ISO)}; ersetzt eine gewählte Option."""
    if richtung not in ("hin", "rueck"):
        raise ValueError("Richtung muss hin oder rueck sein.")

    def f(w):
        w.setdefault("zug", {})[f"{d.isoformat()}:{richtung}"] = zug
        w.get("wahl", {}).pop(f"{d.isoformat()}:{richtung}", None)

    _aendern(f)


# ------------------------------------------------------------ Lesen
def anpassung(w: dict, d: date) -> Optional[dict]:
    return w.get("anpassungen", {}).get(d.isoformat())


def wahl(w: dict, d: date, richtung: str) -> Optional[str]:
    return w.get("wahl", {}).get(f"{d.isoformat()}:{richtung}")


def zug(w: dict, d: date, richtung: str) -> Optional[dict]:
    return w.get("zug", {}).get(f"{d.isoformat()}:{richtung}")


def beschreibung(a: Optional[dict]) -> str:
    """'an 10:00, zurück ab 16:00, Stadtmitte' für Push und Terminal."""
    if not a:
        return ""
    teile = []
    if a.get("an"):
        teile.append(f"an {a['an']}")
    if a.get("rueck_ab"):
        teile.append(f"zurück ab {a['rueck_ab']}")
    if a.get("campus"):
        teile.append(config.einstellungen()["campus"][a["campus"]].get("anzeige", a["campus"]))
    return ", ".join(teile)


def tag(d: date, w: Optional[dict] = None, plan: Optional[dict] = None) -> Tag:
    """Wie stundenplan.tag, aber mit deinen eigenen Zeiten für diesen Tag."""
    t = stundenplan.tag(d, plan)
    a = anpassung(laden() if w is None else w, d)
    if not a:
        return t
    tz = config.zeitzone()

    def zeit(hhmm: Optional[str]) -> Optional[datetime]:
        if not hhmm:
            return None
        h, m = map(int, hhmm.split(":"))
        return datetime(d.year, d.month, d.day, h, m, tzinfo=tz)

    an, rueck = zeit(a.get("an")), zeit(a.get("rueck_ab"))
    if t.frei:
        campus = a.get("campus")
        if not (an and rueck and campus):
            t.warnungen.append("Eigene Zeiten an einem freien Tag brauchen Ankunft, Rückfahrt und Campus.")
            t.eigene = a     # gespeichert, damit der Dienst nicht immer wieder neu plant
            return t
        # Freier Tag, du willst trotzdem hin: ein Termin aus deinen Zeiten
        t = Tag(d, frei=False, termine=[Termin("Eigene Zeit", "", an, rueck, campus)],
                campus=campus, warnungen=t.warnungen)
    t.eigene = a
    t.ziel_beginn = an
    t.rueck_ab = rueck
    if a.get("campus"):
        t.campus = a["campus"]
    beginn = an or t.erster.von
    if rueck is None and t.letzter.bis <= beginn:
        t.warnungen.append(f"Deine Termine enden vor {beginn:%H:%M}, bitte auch die Rückfahrt-Zeit setzen.")
    return t
