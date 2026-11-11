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

import hmac
import json
import logging
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

from . import bewertung, config, stundenplan, takt, texte, wuensche, zustand
from .bewertung import Regeln
from .modell import Verbindung

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
TAGE_ANPASSEN = 8     # so viele Tage ab heute zeigt "Zeiten anpassen"
MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
          "September", "Oktober", "November", "Dezember"]


def _gleis_label(a, an: bool = False) -> str:
    g = a.gleis_an if an else a.gleis_ab
    return f"{'Steig' if a.ist_steig else 'Gleis'} {g}" if g else ""


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
    ziel = "zu Hause" if v.richtung == "rueck" else f"an {texte.kurzname(fahrten[-1].nach.name)}"
    us = bewertung.umstiege(v, regeln)

    abschnitte = []
    for a in fahrten:
        status, art = _status(a, jetzt)
        abschnitte.append({
            "linie": a.linie, "richtung": a.richtung, "steig": a.ist_steig,
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
            "gleis": _gleis_label(a),
            "umstieg": (f"{u.minuten} min Umstieg" + (f" statt {u.minuten_plan}" if u.minuten < u.minuten_plan else ""))
                       if u else "",
            "umstieg_warn": bool(u and (not u.reicht or u.minuten < u.minuten_plan)),
        })
    letzte = fahrten[-1]
    halte.append({"ort": texte.kurzname(letzte.nach.name), "an": f"{letzte.an:%H:%M}",
                  "an_spaet": letzte.verspaetung_an > 0, "ab": "", "ab_alt": "", "ab_spaet": False,
                  "gleis": _gleis_label(letzte, an=True), "umstieg": "", "umstieg_warn": False, "ziel": True})
    lage, lage_art = _lage(v, us)
    return {
        "lage": lage, "lage_art": lage_art,
        "richtung": ("Hinfahrt, " if v.richtung == "hin" else "Rückfahrt, ") + texte.campus_name(v.campus),
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
    }


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
        daten.update({"stadt": takt.info(stadt, o, regeln), "schluessel": k,
                      "gewaehlt": k == g_key and not offen, "wird_gewaehlt": k == offen,
                      "empfohlen": k == empfohlen, "fahrplan": k != g_key})
        ergebnis.append(daten)
    return ergebnis


def _hauptfahrt(p: dict, jetzt: datetime) -> str:
    """Die Fahrt, die aufgeklappt geladen wird: Hinfahrt, bis du angekommen bist."""
    hin = Verbindung.aus_dict(p["hin"]) if p.get("hin") else None
    return "rueck" if hin and hin.an < jetzt else "hin"


def _tafel(p: dict, richtung: str, jetzt: datetime) -> Optional[dict]:
    t = (p.get("tafel") or {}).get(richtung)
    if not t or jetzt - datetime.fromisoformat(t["stand"]) > timedelta(minutes=15):
        return None      # zu alt, lieber nichts zeigen
    return {**t, "stand": datetime.fromisoformat(t["stand"]).strftime("%H:%M"),
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
    for r, titel in (("hin", "Hinfahrt"), ("rueck", "Rückfahrt")):
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
        daten["fahrten"].append({"richtung": r, "titel": titel, "optionen": opt, "zu": False,
                                 "aufklappbar": r != haupt, "tafel": _tafel(p, r, jetzt),
                                 "fehlt": fehlt if not opt else ""})
    return daten


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
            "campi": [{"wert": k, "name": c.get("anzeige", k)} for k, c in e["campus"].items()]}


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
            break
    if kopf is None:
        if p_heute and p_heute["tag"]["frei"]:
            leer = "Heute frei"
        elif p_heute:
            leer = "Für heute erledigt"
        else:
            leer = "Noch kein Plan"
        naechste = zustand.verbindung(z, morgen, "hin")
        kopf = {"leer": leer, "zeile": f"Morgen {naechste.ab:%H:%M} ab Gelnhausen" if naechste else ""}

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
        "tage": tage,
        # Abends ist morgen interessanter als ein erledigtes Heute
        "start_tag": 0 if "leer" not in kopf else 1,
        "anpassen": anpassen_daten(z, w, heute),
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
    a = wuensche.anpassen(d, an=daten.get("an"), rueck_ab=daten.get("rueck_ab"), campus=daten.get("campus"),
                          heim_bis=daten.get("heim_bis"))
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
    tafel = ((p or {}).get("tafel") or {}).get(richtung) if richtung in ("hin", "rueck") else None
    zug = next((z for z in (tafel or {}).get("zuege", [])
                if z["pt"] == pt and z["linie"] == daten.get("linie")), None)
    if zug is None:
        raise ValueError("Diesen Zug kenne ich nicht (mehr), bitte neu laden.")
    if zug["ausfall"]:
        raise ValueError("Dieser Zug fällt aus.")
    ab_plan = datetime.strptime(pt, "%y%m%d%H%M").replace(tzinfo=config.zeitzone())
    wuensche.zug_nehmen(d, richtung, {"halt_id": tafel["halt_id"], "linie": zug["linie"],
                                      "ab_plan": ab_plan.isoformat()})
    return {"ok": True, "text": f"{zug['linie']} {zug['ist']}: Ich plane den Rest der Fahrt, gleich kommt ein Push."}


POST_ROUTEN = {"/api/waehlen": waehlen, "/api/anpassen": anpassen, "/api/zug": zug_nehmen}


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
            # 10 min im Browser behalten: Öffnen geht schneller, ein Deploy wirkt spätestens dann
            self._senden(200, "text/html; charset=utf-8", (STATIC / "index.html").read_bytes(),
                         cache="private, max-age=600")
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
