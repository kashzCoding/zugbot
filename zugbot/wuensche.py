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


# Wählbare Orte je Feld; None = Standard (erster Eintrag)
ORT_FELDER = {
    "hin_von": ("gelnhausen",),
    "hin_nach": ("campus",),
    "rueck_von": ("campus",),
    "rueck_nach": ("gelnhausen", "campus"),
}
STANDARD_ORT = {"hin_von": "gelnhausen", "hin_nach": "campus", "rueck_von": "campus", "rueck_nach": "gelnhausen"}


def erlaubte_orte(feld: str) -> list[str]:
    return list(ORT_FELDER[feld]) + list(config.einstellungen().get("orte") or {})


def ort_name(ort: str, campus: Optional[str] = None) -> str:
    """'gelnhausen' -> 'Gelnhausen', 'campus' -> 'Campus'/'Lichtwiese', 'eberstadt' -> 'Eberstadt'."""
    e = config.einstellungen()
    if ort == "gelnhausen":
        return "Gelnhausen"
    if ort == "campus":
        return e["campus"][campus].get("anzeige", campus) if campus in e["campus"] else "Campus"
    return (e.get("orte") or {}).get(ort, {}).get("anzeige", ort)


def pruefen(d: date, an=None, rueck_ab=None, campus=None, heute: Optional[date] = None,
            heim_bis=None, los_ab=None, **orte) -> dict:
    """Gibt die bereinigte Anpassung zurück oder wirft ValueError mit deutschem Text."""
    heute = heute or _heute()
    if not heute <= d <= heute + timedelta(days=TAGE_VORAUS):
        raise ValueError(f"Eigene Zeiten gehen nur für heute bis in {TAGE_VORAUS} Tagen.")
    a = {"an": _uhrzeit(an, "Ankunft"), "rueck_ab": _uhrzeit(rueck_ab, "Rückfahrt")}
    # Neue Schlüssel nur wenn gesetzt: alte Einträge bleiben gleich (sonst plant der Dienst neu)
    if _uhrzeit(heim_bis, "Zu Hause um"):
        a["heim_bis"] = _uhrzeit(heim_bis, "Zu Hause um")
    if _uhrzeit(los_ab, "Los ab"):
        a["los_ab"] = _uhrzeit(los_ab, "Los ab")
        if a["an"]:
            raise ValueError("Bitte entweder „Da sein um“ oder „Los ab“, nicht beides.")
    for feld in ORT_FELDER:
        wert = orte.pop(feld, None)
        if wert in (None, "", STANDARD_ORT[feld]):
            continue
        if wert not in erlaubte_orte(feld):
            raise ValueError(f"Ort „{wert}“ gibt es hier nicht.")
        a[feld] = wert
    if orte:
        raise ValueError(f"Unbekannte Angabe: {', '.join(orte)}")
    for r in ("hin", "rueck"):
        von = a.get(f"{r}_von", STANDARD_ORT[f"{r}_von"])
        if von == a.get(f"{r}_nach", STANDARD_ORT[f"{r}_nach"]):
            raise ValueError(f"{'Hinfahrt' if r == 'hin' else 'Rückfahrt'}: Start und Ziel sind gleich.")
    if campus not in (None, ""):
        if campus not in config.einstellungen()["campus"]:
            raise ValueError(f"Campus „{campus}“ gibt es nicht.")
        a["campus"] = campus
    else:
        a["campus"] = None
    if a["an"] and a["rueck_ab"] and a["rueck_ab"] <= a["an"]:
        raise ValueError("Die Rückfahrt muss nach der Ankunft liegen.")
    for k, was in (("an", "der Ankunft"), ("rueck_ab", "„Zurück ab“")):
        if a.get("heim_bis") and a[k] and a["heim_bis"] <= a[k]:
            raise ValueError(f"„Zu Hause um“ muss nach {was} liegen.")
    return a


# ------------------------------------------------------------ Schreiben
def anpassen(d: date, an=None, rueck_ab=None, campus=None, heim_bis=None, los_ab=None,
             **orte) -> Optional[dict]:
    """Setzt eigene Zeiten für einen Tag. Alles leer = zurücksetzen."""
    a = pruefen(d, an, rueck_ab, campus, heim_bis=heim_bis, los_ab=los_ab, **orte)
    leer = not any(a.values())

    def f(w):
        w.setdefault("anpassungen", {})
        vorher = w["anpassungen"].get(d.isoformat()) or {}
        if leer:
            w["anpassungen"].pop(d.isoformat(), None)
        else:
            w["anpassungen"][d.isoformat()] = a
        # Neue Zeiten = neue Optionen, eine alte Wahl passt dann nicht mehr.
        # Bleiben Ankunft und Campus gleich, bleibt auch die Hinfahrt (und ihre Wahl).
        hin_gleich = hin_eingaben(vorher) == hin_eingaben(a)
        for r in (("rueck",) if hin_gleich else ("hin", "rueck")):
            w.get("wahl", {}).pop(f"{d.isoformat()}:{r}", None)
        w.get("zug", {}).pop(f"{d.isoformat()}:rueck", None)

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

    # Zeitstempel: jedes Tippen ist ein neuer Wunsch, auch derselbe Zug nach einem Fehlschlag
    zug = {**zug, "um": datetime.now(config.zeitzone()).isoformat(timespec="seconds")}

    def f(w):
        w.setdefault("zug", {})[f"{d.isoformat()}:{richtung}"] = zug
        w.get("wahl", {}).pop(f"{d.isoformat()}:{richtung}", None)

    _aendern(f)


def hin_eingaben(a: Optional[dict]) -> tuple:
    """Was von den eigenen Zeiten die Hinfahrt bestimmt."""
    a = a or {}
    return tuple(a.get(k) for k in ("an", "campus", "los_ab", "hin_von", "hin_nach"))


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
    if a.get("hin_von") or a.get("hin_nach"):
        teile.append(f"hin {ort_name(a.get('hin_von') or 'gelnhausen', a.get('campus'))} → "
                     f"{ort_name(a.get('hin_nach') or 'campus', a.get('campus'))}")
    if a.get("an"):
        teile.append(f"an {a['an']}")
    if a.get("los_ab"):
        teile.append(f"los ab {a['los_ab']}")
    if a.get("rueck_von") or a.get("rueck_nach"):
        teile.append(f"zurück {ort_name(a.get('rueck_von') or 'campus', a.get('campus'))} → "
                     f"{ort_name(a.get('rueck_nach') or 'gelnhausen', a.get('campus'))}")
    if a.get("rueck_ab"):
        teile.append(f"zurück ab {a['rueck_ab']}")
    if a.get("heim_bis"):
        teile.append(f"zu Hause bis {a['heim_bis']}" if not a.get("rueck_nach")
                     else f"in {ort_name(a['rueck_nach'], a.get('campus'))} bis {a['heim_bis']}")
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
    heim, los = zeit(a.get("heim_bis")), zeit(a.get("los_ab"))
    nur_eigene = bool(config.einstellungen()["rueckweg"].get("nur_mit_eigener_zeit"))
    if t.frei:
        campus = a.get("campus")
        # Ohne Campus-Ziel (z. B. Gelnhausen -> Eberstadt) reicht der Campus als Platzhalter
        if not campus and "campus" not in (a.get("hin_nach") or "campus", a.get("rueck_von") or "campus",
                                           a.get("rueck_nach")):
            campus = next(iter(config.einstellungen()["campus"]))
        beginn = an or los
        if not (beginn and campus and (rueck or heim or nur_eigene)):
            t.warnungen.append("Eigene Zeiten an einem freien Tag brauchen „Da sein um“ oder „Los ab“ und Campus"
                               + ("." if nur_eigene else " und Rückfahrt."))
            t.eigene = a     # gespeichert, damit der Dienst nicht immer wieder neu plant
            return t
        # Freier Tag, du willst trotzdem hin: ein Termin aus deinen Zeiten
        t = Tag(d, frei=False, termine=[Termin("Eigene Zeit", "", beginn, rueck or heim or beginn, campus)],
                campus=campus, warnungen=t.warnungen)
    t.eigene = a
    t.ziel_beginn = an
    t.rueck_ab = rueck
    t.heim_bis = heim
    t.los_ab = los
    for feld in ORT_FELDER:
        setattr(t, feld, a.get(feld))
    if a.get("campus"):
        t.campus = a["campus"]
    beginn = an or los or t.erster.von
    if rueck is None and heim is None and not nur_eigene and t.letzter.bis <= beginn:
        t.warnungen.append(f"Deine Termine enden vor {beginn:%H:%M}, bitte auch die Rückfahrt-Zeit setzen.")
    return t
