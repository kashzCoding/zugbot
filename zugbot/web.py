"""Dashboard: kleine Webseite + api/status (JSON), ausgeliefert vom Pi.

Bewusst nur mit der Python-Standardbibliothek (http.server), damit auf dem
Pi Zero kein zusätzliches Framework RAM braucht.

Schreiben kann das Dashboard nur Wünsche (wuensche.py): eine Option wählen
(POST api/waehlen) und eigene Zeiten setzen (POST api/anpassen). Den Plan
selbst ändert danach der Dienst.

Öffentlich erreichbar über Tailscale Funnel. Schutz: Die Seite antwortet nur
unter /<DASHBOARD_SCHLUESSEL>/ (aus der .env), alles andere ist 404. Ohne
Schlüssel in der .env (z. B. am Mac zum Testen) liegt sie unter /.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

from . import bewertung, config, ntfy, stundenplan, takt, texte, wuensche, zustand
from . import tafel as tafel_mod
from .bewertung import Regeln
from .modell import Verbindung, basis_id
from .planer import _nummer

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
# Nur diese Dateien werden ausgeliefert (feste Liste statt Dateisystem-Zugriff)
BILDER = {
    "logo.svg": "image/svg+xml",
    "apple-touch-icon.png": "image/png",   # Symbol für "Zum Home-Bildschirm"
    "favicon-32.png": "image/png",
    "figtree.woff2": "font/woff2",         # Schrift selbst ausliefern statt von Google
}
MAX_ANFRAGE_BYTES = 4096
ABENDS_ERLEDIGT_AB = 21   # ab dann zeigt die blaue Box bei offener Rückfahrt "erledigt" und morgen
TAGE_ANPASSEN = 8     # so viele Tage ab heute zeigt "Zeiten anpassen"
MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
          "September", "Oktober", "November", "Dezember"]


def _gleis_label(a, an: bool = False) -> str:
    g, plan = (a.gleis_an, a.gleis_an_plan) if an else (a.gleis_ab, a.gleis_ab_plan)
    if not g:
        return ""
    return f"{'Steig' if a.ist_steig else 'Gleis'} {g}" + (f" statt {plan}" if _gleis_wechsel(a, an) else "")


def _gleis_wechsel(a, an: bool = False) -> bool:
    g, plan = (a.gleis_an, a.gleis_an_plan) if an else (a.gleis_ab, a.gleis_ab_plan)
    return bool(g and plan and g != plan)


def _status(a, jetzt: datetime) -> tuple[str, str]:
    if a.ausfall:
        return "fällt aus", "ausfall"
    if a.ab_ist is None:
        # weit weg: noch kein Live-Stand zu erwarten; kurz vorher: wirklich keiner da
        return ("Fahrplan" if a.ab_plan - jetzt > timedelta(minutes=90) else "keine Echtzeit"), "plan"
    if a.verspaetung_ab > 0:
        return f"+{a.verspaetung_ab} min", "spaet"
    return "pünktlich", "ok"


def _lage(v: Verbindung, us) -> tuple[str, str]:
    """Kurzstatus für den Zeit-Chip: das Wichtigste zuerst."""
    if any(a.ausfall for a in v.fahrten):
        return "fällt aus", "warn"
    if any(not u.reicht for u in us):
        return "Anschluss weg", "warn"
    plus = v.fahrten[0].verspaetung_ab
    if plus > 0:
        return f"+{plus} min", "spaet"
    if v.fahrten[0].ab_ist is not None:
        return "pünktlich", "ok"
    return "", ""


def fahrt_daten(v: Verbindung, regeln: Regeln, jetzt: datetime) -> dict:
    """Eine Verbindung fürs Dashboard: Kopfzeile, Balken und Zeitstrahl.

    Zeitstrahl = Halte (Abfahrt jedes Fahrzeugs, am Ende die Ankunft) und
    dazwischen die Fahrten. Die Umstiegszeit steht am Halt.
    """
    fahrten = v.fahrten
    gesamt = max(1, sum(max(1, a.fahrzeit_min) for a in fahrten))
    los = round((v.ab - jetzt).total_seconds() / 60)
    if los > 0:
        los_text = f"in {texte.dauer(los)}" if los < 180 else f"um {v.ab:%H:%M}"
    elif v.an > jetzt:
        los_text = f"unterwegs, an {v.an:%H:%M}"
    else:
        los_text = "angekommen"
    ziel = texte.ankunft_text(v) if v.richtung == "rueck" else f"an {texte.kurzname(fahrten[-1].nach.name)}"
    us = bewertung.umstiege(v, regeln)

    abschnitte = []
    for a in fahrten:
        status, art = _status(a, jetzt)
        abschnitte.append({
            "linie": a.linie, "richtung": texte.kurzname(a.richtung), "steig": a.ist_steig,
            "status": status, "status_art": art, "fahrzeit": f"{a.fahrzeit_min} min",
        })
    halte = []
    for i, a in enumerate(fahrten):
        vorher = fahrten[i - 1] if i else None
        u = us[i - 1] if i else None
        halte.append({
            "ort": texte.kurzname(a.von.name),
            "an": f"{vorher.an:%H:%M}" if vorher else "",
            "an_spaet": bool(vorher and vorher.verspaetung_an > 0),
            "ab": f"{a.ab:%H:%M}", "ab_alt": f"{a.ab_plan:%H:%M}" if a.verspaetung_ab > 0 else "",
            "ab_spaet": a.verspaetung_ab > 0 or a.ausfall,
            "gleis": _gleis_label(a), "gleis_wechsel": _gleis_wechsel(a),
            "umstieg": (f"{u.minuten} min Umstieg" + (f" statt {u.minuten_plan}" if u.minuten < u.minuten_plan else ""))
                       if u else "",
            "umstieg_warn": bool(u and (not u.reicht or u.minuten < u.minuten_plan)),
        })
    letzte = fahrten[-1]
    halte.append({"ort": texte.kurzname(letzte.nach.name), "an": f"{letzte.an:%H:%M}",
                  "an_spaet": letzte.verspaetung_an > 0, "ab": "", "ab_alt": "", "ab_spaet": False,
                  "gleis": _gleis_label(letzte, an=True), "gleis_wechsel": _gleis_wechsel(letzte, an=True),
                  "umstieg": "", "umstieg_warn": False, "ziel": True})
    lage, lage_art = _lage(v, us)
    return {
        "lage": lage, "lage_art": lage_art,
        "richtung": ("Hinfahrt, " if v.richtung == "hin" else "Rückfahrt, ") + texte.ziel_name(v),
        "zu_hause": v.ziel_ort == "gelnhausen",
        "los": los_text,
        "ab": f"{v.ab:%H:%M}", "an": f"{v.an:%H:%M}",
        "von": texte.kurzname(fahrten[0].von.name), "gleis": _gleis_label(fahrten[0]),
        "zeile": f"{v.ab:%H:%M} ab {texte.kurzname(fahrten[0].von.name)}, {ziel} {v.an:%H:%M}",
        "meta": f"{texte.dauer(v.dauer_min)} · {v.umstiege} Umstieg{'e' if v.umstiege != 1 else ''}",
        "segmente": [{
            "kurz": a.kurz, "anteil": round(100 * max(1, a.fahrzeit_min) / gesamt, 1),
            "verspaetet": a.verspaetung_an > 0 or a.verspaetung_ab > 0 or a.ausfall,
            "plus": f"+{max(a.verspaetung_ab, a.verspaetung_an)}" if not a.ausfall and
                    max(a.verspaetung_ab, a.verspaetung_an) > 0 else ("fällt aus" if a.ausfall else ""),
        } for a in fahrten],
        "fahrzeit": texte.dauer(v.dauer_min),
        "abschnitte": abschnitte,
        "halte": halte,
        "hinweis": v.hinweis,
        "unterwegs": v.ab <= jetzt < v.an,
        "vorbei": v.an < jetzt,
        "ab_iso": v.ab.isoformat(),
        "schritt": _schritt(v, jetzt),
    }


def _alternativen(p: Optional[dict], richtung: str, jetzt: datetime) -> list[dict]:
    """Knöpfe aus der Überwachung, solange sie noch Sinn haben."""
    return [a for a in ((p or {}).get("alternativen") or {}).get(richtung, [])
            if datetime.fromisoformat(a["bis"]) > jetzt]


def _schritt(v: Verbindung, jetzt: datetime) -> Optional[dict]:
    """Unterwegs: was als Nächstes zu tun ist. Die Seite zählt die Minuten selbst herunter."""
    fahrten = v.fahrten
    if not (v.ab <= jetzt < v.an):
        return None
    for i, a in enumerate(fahrten):
        nxt = fahrten[i + 1] if i + 1 < len(fahrten) else None
        if jetzt < a.ab:        # du wartest auf dieses Fahrzeug
            return {"titel": f"{a.linie} fährt", "zeit_iso": a.ab.isoformat(),
                    "text": f"{a.ab:%H:%M} ab {texte.kurzname(a.von.name)}",
                    "gleis": _gleis_label(a)}
        if jetzt < a.an:        # du sitzt drin
            if nxt is None:
                return {"titel": "Ankunft", "zeit_iso": a.an.isoformat(),
                        "text": f"{a.an:%H:%M} in {texte.kurzname(a.nach.name)}", "gleis": ""}
            warten = round((nxt.ab - a.an).total_seconds() / 60)
            return {"titel": f"Umstieg {texte.kurzname(a.nach.name)}", "zeit_iso": a.an.isoformat(),
                    "text": f"dann {nxt.linie} {nxt.ab:%H:%M}, {warten} min Zeit",
                    "gleis": _gleis_label(nxt)}
    return None


def optionen(p: Optional[dict], d, richtung: str, regeln: Regeln, jetzt: datetime, w: dict) -> list[dict]:
    """Alle Optionen einer Richtung, nach Abfahrt sortiert (Zeit-Chips).

    Die überwachte Verbindung kommt mit Echtzeit aus p[richtung]; die anderen
    sind Fahrplanstand der letzten Planung bzw. mit DB-Echtzeit aufgefrischt
    (dienst._optionen_live). Abgefahrene fallen weg.
    """
    if not p or not p.get(richtung):
        return []
    gewaehlt = Verbindung.aus_dict(p[richtung])
    g_key = gewaehlt.schluessel()
    liste = [Verbindung.aus_dict(o) for o in zustand.alle_optionen(p, richtung)]
    empfohlen = liste[0].schluessel() if liste else g_key
    if g_key not in {o.schluessel() for o in liste}:
        liste.append(gewaehlt)            # z. B. unterwegs neu geplant
    liste = [gewaehlt if o.schluessel() == g_key else o for o in liste]
    wunsch = wuensche.wahl(w, d, richtung)
    offen = wunsch if wunsch and wunsch != (p.get("wahl") or {}).get(richtung) else None
    stadt = (p.get("stadt") or {}).get(richtung)
    ergebnis = []
    for o in sorted(liste, key=lambda x: (x.ab, x.an)):
        k = o.schluessel()
        if k != g_key and o.ab <= jetzt:
            continue
        daten = fahrt_daten(o, regeln, jetzt)
        plan = k == g_key and not o.start and not o.ziel     # Tram wählen nur im Plan, auf dem Standardweg
        info = takt.info(stadt, o, regeln, _stadt_live(d, richtung, jetzt) if plan else None)
        daten.update({"stadt": _stadt_waehlbar(info, o, d, jetzt) if plan else info, "schluessel": k,
                      "gewaehlt": k == g_key and not offen, "wird_gewaehlt": k == offen,
                      "empfohlen": k == empfohlen, "fahrplan": k != g_key})
        ergebnis.append(daten)
    return ergebnis


def _hauptfahrt(p: dict, jetzt: datetime) -> str:
    """Die Fahrt, die aufgeklappt geladen wird: Hinfahrt, bis du angekommen bist."""
    hin = Verbindung.aus_dict(p["hin"]) if p.get("hin") else None
    return "rueck" if hin and hin.an < jetzt else "hin"


_TAFEL_WUNSCH: dict[tuple, dict] = {}    # auf Wunsch geladene Tafeln: (Datum, Richtung) -> Tafel
_gleise = None


def _tafel_roh(p: dict, d, richtung: str, jetzt: datetime) -> Optional[dict]:
    """Die frischeste Tafel: vom Dienst oder auf Wunsch geladen; älter als 15 min zählt nicht."""
    kandidaten = [(p.get("tafel") or {}).get(richtung), _TAFEL_WUNSCH.get((d.isoformat(), richtung))]
    kandidaten = [t for t in kandidaten
                  if t and jetzt - datetime.fromisoformat(t["stand"]) <= timedelta(minutes=15)]
    return max(kandidaten, key=lambda t: t["stand"]) if kandidaten else None


def tafel_laden(daten: dict, jetzt: Optional[datetime] = None) -> dict:
    """"Andere Züge anzeigen": Abfahrtstafel sofort laden, auch Stunden vor der Fahrt."""
    global _gleise
    jetzt = jetzt or datetime.now(config.zeitzone())
    d = wuensche.datum_lesen(str(daten.get("datum", "")), jetzt.date())
    richtung = daten.get("richtung")
    if richtung not in ("hin", "rueck"):
        raise ValueError("Richtung muss hin oder rueck sein.")
    if d != jetzt.date():
        raise ValueError("Die Abfahrtstafel gibt es nur für heute.")
    v = zustand.verbindung(zustand.laden(), d, richtung)
    if v is None:
        raise ValueError("Für diese Fahrt gibt es keinen Plan.")
    e = config.einstellungen()
    if _gleise is None:
        from .gleise import DbGleise
        _gleise = DbGleise(e)
    t = tafel_mod.bauen(v, jetzt, _gleise, Regeln.aus_einstellungen(e), e, immer=True)
    if not t or not t["zuege"]:
        raise ValueError("Für diesen Bahnhof bekomme ich gerade keine Abfahrten.")
    _TAFEL_WUNSCH[(d.isoformat(), richtung)] = t
    return {"ok": True, "text": ""}


# ---------- Tram/Bus Darmstadt Hbf <-> Campus selbst wählen ----------
_STADT_WUNSCH: dict[tuple, dict] = {}    # "Weitere Trams": (Datum, Richtung) -> {"stand", "fahrten"}
STADT_LIVE_MIN = 20                      # so lange gilt eine nachgeladene Liste
STADT_WAEHLBAR_MIN = 20                  # so lange nach der Abfahrt kannst du sie noch als "genommen" eintragen


def _stadt_live(d, richtung: str, jetzt: datetime) -> Optional[list[dict]]:
    t = _STADT_WUNSCH.get((d.isoformat(), richtung))
    if t and jetzt - datetime.fromisoformat(t["stand"]) <= timedelta(minutes=STADT_LIVE_MIN):
        return t["fahrten"]
    return None


def _hbf_id() -> str:
    return config.einstellungen()["haltestellen"]["darmstadt_hbf"]["id"]


def _stadt_waehlbar(info: Optional[dict], v: Verbindung, d, jetzt: datetime) -> Optional[dict]:
    """Jede Tram bekommt "waehlbar" (Knopf "Nehme ich"). Ältere Pläne kennen
    die Steig-ID nicht: hin geht dann Darmstadt Hbf, zurück nicht."""
    if not info:
        return info
    for f in info["fahrten"]:
        if not f["halt_id"] and v.richtung == "hin":
            f["halt_id"] = _hbf_id()
        f["waehlbar"] = bool(f["halt_id"]) and not f["deine"] and \
            datetime.fromisoformat(f["ab_dt"]) >= jetzt - timedelta(minutes=STADT_WAEHLBAR_MIN)
    info["nachladbar"] = d == jetzt.date() and v.an > jetzt
    return info


def _planer_holen():
    global _planer
    if _planer is None:
        from .bahn import Bahn
        from .planer import Planer
        e = config.einstellungen()
        _planer = Planer(Bahn(e), e)
    return _planer


def stadt_laden(daten: dict, jetzt: Optional[datetime] = None) -> dict:
    """"Weitere Trams": aktuelle Trams/Busse zwischen Darmstadt Hbf und Campus,
    hin ab deiner (echten) Zugankunft, zurück ab jetzt."""
    jetzt = jetzt or datetime.now(config.zeitzone())
    d = wuensche.datum_lesen(str(daten.get("datum", "")), jetzt.date())
    richtung = daten.get("richtung")
    if richtung not in ("hin", "rueck"):
        raise ValueError("Richtung muss hin oder rueck sein.")
    if d != jetzt.date():
        raise ValueError("Weitere Trams gibt es nur für heute.")
    v = zustand.verbindung(zustand.laden(), d, richtung)
    if v is None or v.start or v.ziel:
        raise ValueError("Trams kann ich nur auf dem Weg über Darmstadt Hbf laden.")
    regeln = Regeln.aus_einstellungen(config.einstellungen())
    start = jetzt - timedelta(minutes=2)
    if richtung == "hin":
        zug = next((a for a in v.fahrten if a.produkt not in ("tram", "bus")
                    and bewertung.ist_darmstadt_hbf(a.nach, regeln)), None)
        if zug:
            start = max(start, zug.an)
    fahrten = _planer_holen().stadt_suche(richtung, v.campus, start, 90 * 60, anzahl=10)
    if not fahrten:
        raise ValueError("Gerade finde ich keine Trams oder Busse.")
    _STADT_WUNSCH[(d.isoformat(), richtung)] = {"stand": jetzt.isoformat(), "fahrten": fahrten}
    return {"ok": True, "text": ""}


def _stadt_fahrt(p: dict, d, richtung: str, pt: str, linie: str, jetzt: datetime) -> Optional[dict]:
    """Tram/Bus aus der gespeicherten oder nachgeladenen Liste, mit Steig-ID."""
    liste = ((p.get("stadt") or {}).get(richtung) or {}).get("fahrten") or []
    liste = liste + (_stadt_live(d, richtung, jetzt) or [])
    for f in liste:
        if f["linie"] == linie and f"{datetime.fromisoformat(f['ab']):%y%m%d%H%M}" == pt:
            halt_id = f.get("von_id") or (_hbf_id() if richtung == "hin" else "")
            return {"linie": linie, "ist": f"{datetime.fromisoformat(f['ab']):%H:%M}",
                    "ausfall": False, "halt_id": halt_id} if halt_id else None
    return None


# Ankunft am Ziel je Tafel-Zug ("mit diesem Zug wärst du um ... in Gelnhausen").
# Jede Berechnung fragt Transitous, deshalb im Hintergrund und 20 min gemerkt.
_ANKUNFT: dict[tuple, tuple[float, str]] = {}
_ANKUNFT_MERKEN_S = 20 * 60
_ankunft_lock = threading.Lock()
_ankunft_laeuft = False
_planer = None


def _ankuenfte_rechnen(d, richtung: str, halt_id: str, offen: list[tuple]) -> None:
    global _ankunft_laeuft
    try:
        _planer_holen()
        tz = config.zeitzone()
        for linie, pt in offen:
            text = ""
            try:
                v = zustand.verbindung(zustand.laden(), d, richtung)
                ab_plan = datetime.strptime(pt, "%y%m%d%H%M").replace(tzinfo=tz)
                neu = _planer.ab_zug(v, halt_id, linie, ab_plan, datetime.now(tz)) if v else None
                text = f"{neu.an:%H:%M}" if neu else ""
            except Exception as exc:       # nur Zusatzinfo: nie die Seite stören
                log.info("Ankunft für %s %s nicht berechenbar: %s", linie, pt, exc)
            _ANKUNFT[(halt_id, linie, pt)] = (time.monotonic(), text)
    finally:
        with _ankunft_lock:
            _ankunft_laeuft = False


def _mit_ankunft(t: dict, p: dict, d, richtung: str) -> list[dict]:
    """Tafel-Züge mit "heim": Ankunft am Ziel (HH:MM), "" = keine Weiterfahrt, None = wird berechnet."""
    global _ankunft_laeuft
    jetzt_m = time.monotonic()
    v = Verbindung.aus_dict(p[richtung]) if p.get(richtung) else None
    # "dein Zug" aus dem aktuellen Plan, nicht aus der gemerkten Tafel: nach
    # "Nehme ich" muss die Markierung sofort wechseln.
    mein = next((a for a in v.fahrten if a.produkt not in ("tram", "bus")
                 and a.von.basis == basis_id(t["halt_id"])), None) if v else None
    zuege, offen = [], []
    for z in t["zuege"]:
        z = dict(z, deiner=ist_deiner(z, mein))
        alt = _ANKUNFT.get((t["halt_id"], z["linie"], z["pt"]))
        if z.get("deiner") and v:
            z["heim"] = f"{v.an:%H:%M}"
        elif z.get("ausfall"):
            z["heim"] = ""
        elif alt and jetzt_m - alt[0] < _ANKUNFT_MERKEN_S:
            z["heim"] = alt[1]
        else:
            z["heim"] = None
            offen.append((z["linie"], z["pt"]))
        z["vergleich"] = _vergleich(z["heim"], v) if not z["deiner"] else ""
        zuege.append(z)
    if offen:
        with _ankunft_lock:
            starten = not _ankunft_laeuft
            _ankunft_laeuft = True
        if starten:
            threading.Thread(target=_ankuenfte_rechnen, args=(d, richtung, t["halt_id"], offen),
                             daemon=True).start()
    return zuege


def ist_deiner(z: dict, mein) -> bool:
    """Die einzige Stelle, die "dein Zug" auf der Tafel festlegt. Mit DB-Daten
    über denselben Tafeleintrag, den die Verbindung nutzt (gleise.waehle), dann
    stimmen Gleis und Zeit oben und in der Tafel immer überein. Ohne DB-Eintrag
    (z. B. ohne Schlüssel) nach Minute und Linie."""
    if mein is None:
        return False
    if mein.db_id_ab and z.get("id"):
        return z["id"] == mein.db_id_ab
    return z["pt"] == f"{mein.ab_plan:%y%m%d%H%M}" and _nummer(z["linie"]) == _nummer(mein.linie)


def _vergleich(heim: Optional[str], v: Optional[Verbindung]) -> str:
    """'+37 min' / '8 min früher' / 'gleich' gegenüber deiner Ankunft."""
    if not heim or v is None:
        return ""
    h, m = map(int, heim.split(":"))
    an = v.an.replace(hour=h, minute=m, second=0, microsecond=0)
    if an < v.an - timedelta(hours=12):      # nach Mitternacht
        an += timedelta(days=1)
    diff = round((an - v.an.replace(second=0, microsecond=0)).total_seconds() / 60)
    if diff == 0:
        return "gleich"
    return f"+{diff} min" if diff > 0 else f"{-diff} min früher"


def _tafel(p: dict, d, richtung: str, jetzt: datetime) -> Optional[dict]:
    t = _tafel_roh(p, d, richtung, jetzt)
    if not t:
        return None      # zu alt, lieber nichts zeigen
    return {**t, "zuege": _mit_ankunft(t, p, d, richtung),
            "ziel_name": texte.ziel_name(Verbindung.aus_dict(p[richtung])) if p.get(richtung)
                         else ("Gelnhausen" if richtung == "rueck" else "Campus"),
            "stand": datetime.fromisoformat(t["stand"]).strftime("%H:%M"),
            "fehler": (p.get("zug_fehler") or {}).get(richtung, "")}


def tag_daten(p: Optional[dict], d, name: str, regeln: Regeln, e: dict, jetzt: datetime, w: dict,
              offen: frozenset = frozenset()) -> dict:
    """Ein Tag (Reiter Heute/Morgen): Termine, eigene Zeiten, Hin- und Rückfahrt.

    Nur die Hauptfahrt (bzw. was du aufgeklappt hast, `offen`) kommt mit allen
    Optionen und Zeitstrahl; die andere nur als eine Zeile. Das hält api/status
    klein und schnell."""
    daten = {"name": name, "datum": d.isoformat(),
             "titel": f"{stundenplan.WOCHENTAGE_LANG[d.weekday()]}, {d.day}. {MONATE[d.month - 1]}"}
    if not p:
        return {**daten, "frei": False, "fehlt": "Noch kein Plan, kommt gleich.", "fahrten": []}
    if p["tag"]["frei"]:
        return {**daten, "frei": True, "grund": p["tag"].get("grund", ""), "fahrten": [],
                "eigene": wuensche.beschreibung(p["tag"].get("eigene"))}
    termin = p["tag"]["termine"][0]
    t_von = datetime.fromisoformat(termin["von"])
    daten.update({
        "frei": False, "campus": texte.campus_name(p["tag"]["campus"]),
        "termin": f"{t_von:%H:%M} {termin['titel']}, {termin['raum']}".rstrip(", ")
                  if termin["titel"] != "Eigene Zeit" else "",
        "eigene": wuensche.beschreibung(p["tag"].get("eigene")),
        "warnungen": p["tag"].get("warnungen", []),
        "fahrten": [],
    })
    if p.get("hin"):
        push = Verbindung.aus_dict(p["hin"]).ab_plan - timedelta(
            minutes=e["benachrichtigung"]["morgen_push_minuten_vor_abfahrt"])
        if push > jetzt:
            daten["push"] = f"Push um {push:%H:%M}"
    haupt = _hauptfahrt(p, jetzt)
    eigene = p["tag"].get("eigene") or {}
    wunsch = wuensche.anpassung(w, d) or {}       # schon gespeichert, vom Dienst evtl. noch nicht geplant
    for r, titel in (("hin", "Hinfahrt"), ("rueck", "Rückfahrt")):
        if r == "rueck" and p.get("rueck_offen"):
            ausstehend = bool(wunsch.get("rueck_ab") or wunsch.get("heim_bis"))
            # eingeklappt wie eine Nebenfahrt: bis du angekommen bist oder sie aufklappst
            zu = r != haupt and f"{d.isoformat()}:{r}" not in offen
            daten["fahrten"].append({"richtung": r, "titel": titel, "zu": zu, "optionen": [],
                                     "aufklappbar": True,     # auch als Hauptfahrt (Seite merkt sich "zu")
                                     "kurz": {"text": "wird berechnet …" if ausstehend else "noch keine Zeit",
                                              "vorbei": False, "lage": ""},
                                     "rueck_offen": True,
                                     # für eine noch alte Seite im Zwischenspeicher
                                     "fehlt": "Noch keine Rückfahrtzeit. Bitte Seite neu laden, um sie einzutragen.",
                                     "vorschlaege": _rueck_vorschlaege(p, jetzt),
                                     "ausstehend": ausstehend})
            continue
        fehlt = "" if p.get(r) else (p.get("fehler") or f"Keine passende {titel} gefunden.")
        if r != haupt and f"{d.isoformat()}:{r}" not in offen and p.get(r):
            v = Verbindung.aus_dict(p[r])
            lage, lage_art = _lage(v, bewertung.umstiege(v, regeln))
            daten["fahrten"].append({"richtung": r, "titel": titel, "zu": True, "fehlt": "",
                                     "kurz": {"ab": f"{v.ab:%H:%M}", "an": f"{v.an:%H:%M}",
                                              "lage": lage, "lage_art": lage_art,
                                              "vorbei": v.an < jetzt}})
            continue
        opt = optionen(p, d, r, regeln, jetzt, w)
        if r == "rueck" and (eigene.get("rueck_ab") or eigene.get("heim_bis")):
            daten["rueck_zeit"] = (f"Zurück ab {eigene['rueck_ab']}" if eigene.get("rueck_ab")
                                   else f"Zu Hause bis {eigene['heim_bis']}" if not eigene.get("rueck_nach")
                                   else f"Spätestens an {eigene['heim_bis']}")
        daten["fahrten"].append({"richtung": r, "titel": titel, "optionen": opt, "zu": False,
                                 "rueck_zeit": daten.get("rueck_zeit", "") if r == "rueck" else "",
                                 "aufklappbar": r != haupt, "tafel": _tafel(p, d, r, jetzt),
                                 "tafel_ladbar": d == jetzt.date() and bool(p.get(r))
                                                 and Verbindung.aus_dict(p[r]).an > jetzt,
                                 "fehlt": fehlt if not opt else ""})
    return daten


def _rueck_vorschlaege(p: dict, jetzt: datetime) -> list[dict]:
    """Schnellwahl auf der Rückfahrt-Karte: Vorlesungsende + Weg zur Haltestelle, dann
    halbstündlich. Ohne echte Vorlesung (nur eigene Zeit) gibt es kein Ende zum Vorschlagen."""
    termine = [t for t in p["tag"].get("termine") or [] if t["titel"] != "Eigene Zeit"]
    if not termine:
        return []
    ende = max(datetime.fromisoformat(t["bis"]) for t in termine)
    ende += timedelta(minutes=config.einstellungen()["rueckweg"]["frueheste_abfahrt_nach_ende_minuten"])
    zeiten = [ende + timedelta(minutes=m) for m in (0, 30, 60, 90)]
    return [{"zeit": f"{z:%H:%M}", "text": f"{z:%H:%M}" + (" · nach Vorlesung" if z == ende else "")}
            for z in zeiten if z.date() == ende.date() and z > jetzt]


def anpassen_daten(z: dict, w: dict, heute) -> dict:
    """Für den Bereich "Zeiten anpassen": die nächsten Tage mit Stundenplan und eigenen Zeiten."""
    e = config.einstellungen()
    plan = config.stundenplan_roh()
    tage = []
    for i in range(TAGE_ANPASSEN):
        d = heute + timedelta(days=i)
        t = stundenplan.tag(d, plan)
        if t.frei:
            standard = f"frei ({t.grund})" if t.grund else "frei"
        else:
            standard = (f"{t.erster.von:%H:%M} bis {t.letzter.bis:%H:%M}, {texte.campus_name(t.campus)}")
        name = "Heute" if i == 0 else "Morgen" if i == 1 else stundenplan.WOCHENTAGE_LANG[d.weekday()]
        eigene = wuensche.anpassung(w, d)
        p = zustand.plan(z, d)
        tage.append({
            "datum": d.isoformat(), "name": f"{name}, {d.day}.{d.month}.",
            "standard": standard, "eigene": eigene, "eigene_text": wuensche.beschreibung(eigene),
            "beginn": None if t.frei else f"{t.erster.von:%H:%M}",
            "ende": None if t.frei else f"{t.letzter.bis:%H:%M}",
            "ausstehend": bool(p) and (p["tag"].get("eigene") != eigene),
        })
    return {"tage": tage,
            "campi": [{"wert": k, "name": c.get("anzeige", k)} for k, c in e["campus"].items()],
            # je Feld: erster Eintrag ist der Standard
            "orte": {f: [{"wert": o, "name": wuensche.ort_name(o)} for o in wuensche.erlaubte_orte(f)]
                     for f in wuensche.ORT_FELDER}}


def wecker_daten(jetzt: Optional[datetime] = None) -> dict:
    """Weckzeit für morgen: Abfahrt in Gelnhausen minus Vorlauf (einstellungen.yaml).

    Liest nur den gespeicherten Plan, fragt keine Bahndaten ab. Gedacht z. B.
    für einen iPhone-Kurzbefehl, der abends den Wecker stellt.
    """
    e = config.einstellungen()
    vorlauf = e.get("wecker", {}).get("vorlauf_minuten", 40)
    jetzt = jetzt or datetime.now(config.zeitzone())
    morgen = jetzt.date() + timedelta(days=1)
    p = zustand.plan(zustand.laden(), morgen)
    daten = {
        "datum": morgen.isoformat(),
        "wochentag": stundenplan.WOCHENTAGE_LANG[morgen.weekday()],
        "vorlauf_minuten": vorlauf,
    }
    if p is None:
        # Dienst hat morgen noch nicht geplant (passiert direkt nach dem Start kurz)
        return {**daten, "status": "noch_kein_plan", "frei": None, "weckzeit": None}
    if p["tag"]["frei"]:
        return {**daten, "status": "frei", "frei": True, "grund": p["tag"]["grund"], "weckzeit": None}
    termin = p["tag"]["termine"][0]
    daten.update({
        "frei": False,
        "campus": texte.campus_name(p["tag"]["campus"]),
        "erster_termin": {"beginn": datetime.fromisoformat(termin["von"]).strftime("%H:%M"),
                          "titel": termin["titel"], "raum": termin["raum"]},
        "eigene_zeiten": p["tag"].get("eigene"),
    })
    if not p.get("hin"):
        return {**daten, "status": "keine_verbindung", "weckzeit": None,
                "hinweis": p.get("fehler") or "Keine passende Hinfahrt gefunden."}
    erste = Verbindung.aus_dict(p["hin"]).fahrten[0]
    wecken = erste.ab - timedelta(minutes=vorlauf)
    return {
        **daten,
        "status": "ok",
        "weckzeit": f"{wecken:%H:%M}",
        "weckzeit_iso": wecken.isoformat(),
        "abfahrt": f"{erste.ab:%H:%M}",
        "abfahrt_iso": erste.ab.isoformat(),
        "linie": erste.linie,
        "gleis": erste.gleis_ab,
        "geplant_um": p.get("geplant_um"),
    }


def seite_mit_version() -> tuple[str, bytes]:
    """index.html mit eingesetzter Version (Hash des Inhalts). Die Seite vergleicht sie
    mit api/status und lädt sich nach einem Deploy selbst neu."""
    roh = (STATIC / "index.html").read_bytes()
    version = hashlib.sha256(roh).hexdigest()[:12]
    return version, roh.replace(b"%%SEITEN_VERSION%%", version.encode())


def dashboard_daten(offen: frozenset = frozenset()) -> dict:
    e = config.einstellungen()
    regeln = Regeln.aus_einstellungen(e)
    tz = config.zeitzone()
    jetzt = datetime.now(tz)
    z = zustand.laden()
    w = wuensche.laden()
    heute, morgen = jetzt.date(), jetzt.date() + timedelta(days=1)
    status = z.get("status", {})
    modus = status.get("modus", "normal")
    p_heute, p_morgen = zustand.plan(z, heute), zustand.plan(z, morgen)

    # Kopfkarte: die nächste oder laufende Fahrt heute
    kopf = None
    for r in ("hin", "rueck"):
        v = zustand.verbindung(z, heute, r)
        if v and v.an >= jetzt:
            kopf = fahrt_daten(v, regeln, jetzt)
            kopf.update({"datum": heute.isoformat(), "r": r,
                         "alternativen": [dict(a, text=(
                             f"Anschluss nicht bekommen? {a['linie']} {a['ab']}" + (f", an {a['an']}" if a["an"] else "")
                             if a["art"] == "plan_b" else f"{a['linie']} {a['ab']} doch bekommen?"),
                             knopf=f"{a['linie']} nehmen" if a["art"] == "plan_b" else "Ja, bin drin")
                             for a in _alternativen(p_heute, r, jetzt)]})
            break
    if kopf is None:
        kopf_extra = {}
        if p_heute and p_heute["tag"]["frei"]:
            leer = "Heute frei"
        elif p_heute and p_heute.get("rueck_offen") and jetzt.hour < ABENDS_ERLEDIGT_AB:
            leer = "Rückfahrt noch offen"
            kopf_extra = {"zeile": "Tippen und Zeit eintragen", "datum": heute.isoformat(), "r": "rueck"}
        elif p_heute:
            leer = "Für heute erledigt"
        else:
            leer = "Noch kein Plan"
        naechste = zustand.verbindung(z, morgen, "hin")
        kopf = {"leer": leer, "zeile": f"Morgen {naechste.ab:%H:%M} ab {texte.kurzname(naechste.fahrten[0].von.name)}"
                if naechste else "",
                **kopf_extra}

    tage = [tag_daten(p_heute, heute, "Heute", regeln, e, jetzt, w, offen),
            tag_daten(p_morgen, morgen, "Morgen", regeln, e, jetzt, w, offen)]
    selbsttest = (status.get("selbsttest") or {}).get("fehler") or []
    geprueft = datetime.fromisoformat(status["takt_um"]).strftime("%H:%M") if status.get("takt_um") else ""
    return {
        "datum": f"{stundenplan.WOCHENTAGE_LANG[heute.weekday()]}, {heute.day}. {MONATE[heute.month - 1]}",
        # "Live" kurz vor und während der Fahrt (enger Takt), sonst nur wann zuletzt geprüft
        "modus": ("Live · " if modus == "nah" else "") + (f"geprüft {geprueft}" if geprueft else "startet"),
        "stoerung": "; ".join(selbsttest) or
                    ("Bahndaten gerade nicht erreichbar, Stand kann veraltet sein." if status.get("fehler") else ""),
        "aktualisiert": datetime.fromisoformat(status["takt_um"]).strftime("%H:%M") if status.get("takt_um") else "",
        "kopf": kopf,
        "meldungen": [{"um": m["um"][11:16], "titel": m["titel"], "text": m["text"], "prio": m.get("prio", "")}
                      for m in reversed(ntfy.meldungen()) if m["um"][:10] == heute.isoformat()][:12],
        "tage": tage,
        # Abends ist morgen interessanter als ein erledigtes Heute
        "start_tag": 0 if "leer" not in kopf or kopf.get("r") else 1,
        "anpassen": anpassen_daten(z, w, heute),
        "version": seite_mit_version()[0],
    }


# ------------------------------------------------------------ Schreiben
def waehlen(daten: dict, jetzt: Optional[datetime] = None) -> dict:
    jetzt = jetzt or datetime.now(config.zeitzone())
    d = wuensche.datum_lesen(str(daten.get("datum", "")), jetzt.date())
    richtung, schluessel = daten.get("richtung"), daten.get("schluessel")
    p = zustand.plan(zustand.laden(), d)
    if richtung not in ("hin", "rueck") or not isinstance(schluessel, str) or not p:
        raise ValueError("Diese Verbindung kenne ich nicht.")
    for o in zustand.alle_optionen(p, richtung) + ([p[richtung]] if p.get(richtung) else []):
        v = Verbindung.aus_dict(o)
        if v.schluessel() == schluessel:
            if v.ab <= jetzt:
                raise ValueError("Diese Verbindung ist schon abgefahren.")
            wuensche.waehlen(d, richtung, schluessel)
            return {"ok": True, "text": f"{v.ab:%H:%M} wird übernommen."}
    raise ValueError("Diese Verbindung gibt es nicht mehr, bitte neu laden.")


def anpassen(daten: dict, jetzt: Optional[datetime] = None) -> dict:
    jetzt = jetzt or datetime.now(config.zeitzone())
    d = wuensche.datum_lesen(str(daten.get("datum", "")), jetzt.date())
    if daten.get("zuruecksetzen"):
        wuensche.zuruecksetzen(d)
        return {"ok": True, "text": "Wieder nach Stundenplan, wird neu geplant."}
    if daten.get("nur_rueck"):
        # Rückfahrt-Karte: nur "Zurück ab" setzen oder löschen, der Rest bleibt
        alt = wuensche.anpassung(wuensche.laden(), d) or {}
        rueck = str(daten.get("rueck_ab") or "")
        rest = {k: v for k, v in alt.items() if k not in ("rueck_ab", "heim_bis")}
        a = wuensche.anpassen(d, rueck_ab=rueck, heim_bis=alt.get("heim_bis") if rueck else None, **rest)
        if not rueck:
            return {"ok": True, "text": "Rückfahrtzeit gelöscht."}
        return {"ok": True, "text": f"Rückfahrt ab {wuensche.pruefen(d, rueck_ab=rueck, heute=jetzt.date())['rueck_ab']}, wird berechnet."}
    a = wuensche.anpassen(d, an=daten.get("an"), rueck_ab=daten.get("rueck_ab"), campus=daten.get("campus"),
                          heim_bis=daten.get("heim_bis"), los_ab=daten.get("los_ab"),
                          **{k: daten.get(k) for k in wuensche.ORT_FELDER})
    if a is None:
        return {"ok": True, "text": "Wieder nach Stundenplan, wird neu geplant."}
    hinweis = "" if d <= jetzt.date() + timedelta(days=1) else \
        " Geplant wird ab dem Vortag."   # der Dienst plant nur heute und morgen
    return {"ok": True, "text": f"Gespeichert ({wuensche.beschreibung(a)}), wird neu geplant.{hinweis}"}


def zug_nehmen(daten: dict, jetzt: Optional[datetime] = None) -> dict:
    """"Ich nehme diesen Zug" von der Abfahrtstafel."""
    jetzt = jetzt or datetime.now(config.zeitzone())
    d = wuensche.datum_lesen(str(daten.get("datum", "")), jetzt.date())
    richtung, pt = daten.get("richtung"), str(daten.get("pt", ""))
    p = zustand.plan(zustand.laden(), d)
    tafel = _tafel_roh(p or {}, d, richtung, jetzt) if richtung in ("hin", "rueck") else None
    zug = next((z for z in (tafel or {}).get("zuege", [])
                if z["pt"] == pt and z["linie"] == daten.get("linie")), None)
    halt_id = (tafel or {}).get("halt_id")
    if zug is None:
        # Knöpfe in der Kopfkarte: Ausweichzug ("nicht bekommen") oder alter Zug ("doch bekommen")
        alt = next((a for a in _alternativen(p, richtung, jetzt)
                    if a["pt"] == pt and a["linie"] == daten.get("linie")), None)
        if alt:
            zug, halt_id = {"linie": alt["linie"], "ist": alt["ab"], "ausfall": False}, alt["halt_id"]
    if zug is None and p and richtung in ("hin", "rueck"):
        # Tram/Bus zwischen Darmstadt Hbf und Campus
        zug = _stadt_fahrt(p, d, richtung, pt, str(daten.get("linie", "")), jetzt)
        if zug:
            halt_id = zug["halt_id"]
    if zug is None:
        raise ValueError("Diesen Zug kenne ich nicht (mehr), bitte neu laden.")
    if zug["ausfall"]:
        raise ValueError("Dieser Zug fällt aus.")
    ab_plan = datetime.strptime(pt, "%y%m%d%H%M").replace(tzinfo=config.zeitzone())
    wuensche.zug_nehmen(d, richtung, {"halt_id": halt_id, "linie": zug["linie"],
                                      "ab_plan": ab_plan.isoformat()})
    return {"ok": True, "text": f"{zug['linie']} {zug['ist']}: Ich plane den Rest der Fahrt, gleich kommt ein Push."}


POST_ROUTEN = {"/api/waehlen": waehlen, "/api/anpassen": anpassen, "/api/zug": zug_nehmen,
               "/api/tafel": tafel_laden, "/api/trams": stadt_laden}


class Handler(BaseHTTPRequestHandler):
    schluessel = ""   # wird in serve() aus der .env gesetzt

    def _senden(self, code: int, typ: str, daten: bytes, extra: Optional[dict] = None,
                cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", typ)
        self.send_header("Cache-Control", cache)
        # Schlüssel steht in der URL: nie als Referer an fremde Seiten schicken,
        # nicht in Suchmaschinen, nicht in fremde Seiten einbetten
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Length", str(len(daten)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(daten)

    def _innerer_pfad(self) -> Optional[str]:
        """Pfad hinter dem Schlüssel, oder None, wenn der Schlüssel nicht passt."""
        pfad = self.path.split("?")[0]
        if not self.schluessel:
            return pfad
        teile = pfad.split("/", 2)          # "", schluessel, rest
        if len(teile) < 2 or not hmac.compare_digest(teile[1], self.schluessel):
            return None
        if len(teile) == 2:
            return ""                        # /<schluessel> ohne Schrägstrich
        return "/" + teile[2]

    def do_GET(self):
        pfad = self._innerer_pfad()
        if pfad is None:
            self._senden(404, "text/plain; charset=utf-8", "nicht gefunden".encode())
        elif pfad == "":
            # mit Schrägstrich, damit "api/status" relativ dazu aufgelöst wird
            self._senden(301, "text/plain", b"", {"Location": f"/{self.schluessel}/"})
        elif pfad in ("/", "/index.html"):
            # Jedes Mal beim Pi nachfragen (ETag): unverändert kommt nur "304", nach
            # einem Deploy sofort die neue Seite statt der alten aus dem Zwischenspeicher
            version, seite = seite_mit_version()
            etag = f'"{version}"'
            # Welche Seite hat das Handy? (Fehlersuche Zwischenspeicher; ohne Schlüssel und IP)
            log.debug("Seite abgerufen: hat %s, aktuell %s, %s", self.headers.get("If-None-Match") or "nichts",
                     etag, (self.headers.get("User-Agent") or "")[:60])
            if self.headers.get("If-None-Match") == etag:
                self._senden(304, "text/html; charset=utf-8", b"", {"ETag": etag}, cache="private, no-cache")
            else:
                self._senden(200, "text/html; charset=utf-8", seite, {"ETag": etag}, cache="private, no-cache")
        elif pfad == "/api/wecker":
            try:
                daten = json.dumps(wecker_daten(), ensure_ascii=False).encode()
                self._senden(200, "application/json; charset=utf-8", daten)
            except Exception:
                log.exception("Wecker-Daten fehlgeschlagen")
                self._senden(500, "application/json", b'{"fehler": "intern"}')
        elif pfad.lstrip("/") in BILDER:
            name = pfad.lstrip("/")
            self._senden(200, BILDER[name], (STATIC / name).read_bytes(), cache="private, max-age=604800")
        elif pfad == "/api/status":
            try:
                # ?offen=2026-11-09:rueck,... : diese Fahrten zusätzlich ausführlich
                anfrage = parse_qs(self.path.partition("?")[2])
                offen = frozenset(x for x in ",".join(anfrage.get("offen", [])).split(",") if x)
                daten = json.dumps(dashboard_daten(offen), ensure_ascii=False).encode()
                self._senden(200, "application/json; charset=utf-8", daten)
            except Exception:
                log.exception("Dashboard-Daten fehlgeschlagen")
                self._senden(500, "application/json", b'{"fehler": "intern"}')
        else:
            self._senden(404, "text/plain; charset=utf-8", "nicht gefunden".encode())

    def _json(self, code: int, daten: dict) -> None:
        self._senden(code, "application/json; charset=utf-8", json.dumps(daten, ensure_ascii=False).encode())

    def do_POST(self):
        pfad = self._innerer_pfad()
        if pfad is None or pfad not in POST_ROUTEN:
            self._senden(404, "text/plain; charset=utf-8", "nicht gefunden".encode())
            return
        # Nur JSON: ein Formular einer fremden Seite kann diesen Typ nicht ohne Weiteres senden
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self._json(415, {"fehler": "Bitte als JSON senden."})
            return
        try:
            laenge = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            laenge = -1
        if not 0 < laenge <= MAX_ANFRAGE_BYTES:
            self._json(413 if laenge > MAX_ANFRAGE_BYTES else 400, {"fehler": "Anfrage ungültig."})
            return
        try:
            daten = json.loads(self.rfile.read(laenge))
            if not isinstance(daten, dict):
                raise ValueError("Anfrage ungültig.")
            self._json(200, POST_ROUTEN[pfad](daten))
        except json.JSONDecodeError:
            self._json(400, {"fehler": "Anfrage ungültig."})
        except ValueError as exc:
            self._json(400, {"fehler": str(exc)})
        except Exception:
            log.exception("POST %s fehlgeschlagen", pfad)
            self._json(500, {"fehler": "intern"})

    def log_message(self, fmt, *args):   # Zugriffe nicht ins Log schreiben
        pass


def serve() -> None:
    env = config.env()
    port = config.einstellungen()["web"]["port"]
    Handler.schluessel = env.dashboard_schluessel
    if not env.dashboard_schluessel and env.web_host not in ("127.0.0.1", "localhost"):
        log.warning("DASHBOARD_SCHLUESSEL fehlt, Dashboard ist ungeschützt erreichbar")
    log.info("Dashboard auf http://%s:%s/%s", env.web_host, port,
             "<schlüssel>/" if env.dashboard_schluessel else "")
    ThreadingHTTPServer((env.web_host, port), Handler).serve_forever()
