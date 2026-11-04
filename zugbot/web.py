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
}
MAX_ANFRAGE_BYTES = 4096
TAGE_ANPASSEN = 8     # so viele Tage ab heute zeigt "Zeiten anpassen"
MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
          "September", "Oktober", "November", "Dezember"]


def _gleis_label(a, an: bool = False) -> str:
    g = a.gleis_an if an else a.gleis_ab
    return f"{'Steig' if a.ist_steig else 'Gleis'} {g}" if g else ""


def fahrt_daten(v: Verbindung, regeln: Regeln, jetzt: datetime) -> dict:
    fahrten = v.fahrten
    gesamt = max(1, sum(max(1, a.fahrzeit_min) for a in fahrten))
    los = round((v.ab - jetzt).total_seconds() / 60)
    if los > 0:
        los_text = f"Los in {texte.dauer(los)}" if los < 180 else f"Los um {v.ab:%H:%M}"
    elif v.an > jetzt:
        los_text = f"Unterwegs, an {v.an:%H:%M}"
    else:
        los_text = "Angekommen"
    ziel = "zu Hause" if v.richtung == "rueck" else f"an {texte.kurzname(fahrten[-1].nach.name)}"

    abschnitte = []
    for a in fahrten:
        if a.ausfall:
            status, art = "fällt aus", "ausfall"
        elif a.ab_ist is None:
            status, art = "Fahrplan", "plan"
        elif a.verspaetung_ab > 0:
            status, art = f"+{a.verspaetung_ab} min", "spaet"
        else:
            status, art = "pünktlich", "ok"
        abschnitte.append({
            "ab": f"{a.ab:%H:%M}",
            "ab_alt": f"{a.ab_plan:%H:%M}" if a.verspaetung_ab > 0 else "",
            "linie": f"{a.linie} {texte.kurzname(a.von.name)} → {texte.kurzname(a.nach.name)}",
            "status": status, "status_art": art,
            "an": f"an {a.an:%H:%M}",
            "gleis": _gleis_label(a),
            "fahrzeit": f"{a.fahrzeit_min} min Fahrt",
        })
    umstiege = []
    for u in bewertung.umstiege(v, regeln):
        if u.minuten < u.minuten_plan:
            umstiege.append({"text": f"Umstieg {u.minuten} min statt {u.minuten_plan}", "warn": True})
        else:
            umstiege.append({"text": f"Umstieg {u.minuten} min", "warn": not u.reicht})
    return {
        "richtung": ("Hinfahrt, " if v.richtung == "hin" else "Rückfahrt, ") + texte.campus_name(v.campus),
        "los": los_text,
        "zeile": f"{v.ab:%H:%M} ab {texte.kurzname(fahrten[0].von.name)}, {ziel} {v.an:%H:%M}",
        "segmente": [{
            "kurz": a.kurz, "anteil": round(100 * max(1, a.fahrzeit_min) / gesamt, 1),
            "verspaetet": a.verspaetung_an > 0 or a.verspaetung_ab > 0 or a.ausfall,
            "plus": f"+{max(a.verspaetung_ab, a.verspaetung_an)}" if not a.ausfall and
                    max(a.verspaetung_ab, a.verspaetung_an) > 0 else ("fällt aus" if a.ausfall else ""),
        } for a in fahrten],
        "fahrzeit": texte.dauer(v.dauer_min),
        "abschnitte": abschnitte,
        "umstiege": umstiege,
        "hinweis": v.hinweis,
    }


def morgen_daten(p: Optional[dict], d, regeln: Regeln, e: dict) -> dict:
    wt = stundenplan.WOCHENTAGE_LANG[d.weekday()]
    if not p or p["tag"]["frei"]:
        return {"wochentag": wt, "frei": True, "grund": (p or {}).get("tag", {}).get("grund", "")}
    v = Verbindung.aus_dict(p["hin"]) if p.get("hin") else None
    termin = p["tag"]["termine"][0]
    t_von = datetime.fromisoformat(termin["von"])
    eigene = p["tag"].get("eigene") or {}
    daten = {"wochentag": wt, "frei": False, "campus": texte.campus_name(p["tag"]["campus"]),
             "termin": f"{t_von:%H:%M} {termin['titel']}, {termin['raum']}".rstrip(", "),
             "eigene": f"Eigene Zeit: {wuensche.beschreibung(eigene)}" if eigene else ""}
    if v is None:
        daten["fehlt"] = p.get("fehler") or "Keine Verbindung gefunden."
        return daten
    push = v.ab_plan - timedelta(minutes=e["benachrichtigung"]["morgen_push_minuten_vor_abfahrt"])
    daten.update(kompakt(v, regeln, (p.get("stadt") or {}).get("hin")))
    daten["push"] = f"Morgen-Push um {push:%H:%M}"
    return daten


def _umstieg_text(v: Verbindung, regeln: Regeln) -> str:
    ffm = next((u for u in bewertung.umstiege(v, regeln) if "frankfurt" in u.ort.lower()), None)
    return f"Umstieg {texte.kurzname(ffm.ort)} {ffm.minuten} min" if ffm else f"{v.umstiege} Umstieg(e)"


def kompakt(v: Verbindung, regeln: Regeln, stadt: Optional[dict]) -> dict:
    """Kurzform einer Verbindung (Karte "Morgen", wischbare Optionen)."""
    erste = v.fahrten[0]
    return {
        "ab": f"{erste.ab:%H:%M}", "gleis": _gleis_label(erste),
        "von": texte.kurzname(erste.von.name),
        "an": f"an {v.an:%H:%M}", "dauer": texte.dauer(v.dauer_min),
        "umstieg": _umstieg_text(v, regeln),
        "linien": " › ".join(a.linie for a in v.fahrten),
        "stadt": takt.info(stadt, v, regeln),
    }


def optionen(p: Optional[dict], d, richtung: str, regeln: Regeln, jetzt: datetime,
             w: dict, voll: bool = False) -> list[dict]:
    """Alle Optionen einer Richtung, nach Abfahrt sortiert (zum Wischen).

    Die überwachte Verbindung kommt mit Echtzeit aus p[richtung]; die anderen
    sind Fahrplanstand der letzten Planung. Abgefahrene fallen weg.
    """
    if not p or not p.get(richtung):
        return []
    gewaehlt = Verbindung.aus_dict(p[richtung])
    g_key = gewaehlt.schluessel()
    liste = [Verbindung.aus_dict(o) for o in (p.get("optionen") or {}).get(richtung, [])]
    empfohlen = liste[0].schluessel() if liste else g_key
    if g_key not in {o.schluessel() for o in liste}:
        liste.append(gewaehlt)            # z. B. unterwegs neu geplant
    liste = [gewaehlt if o.schluessel() == g_key else o for o in liste]
    wunsch = wuensche.wahl(w, d, richtung)
    offen = wunsch if wunsch and wunsch != (p.get("wahl") or {}).get(richtung) else None
    stadt = (p.get("stadt") or {}).get(richtung)
    ergebnis = []
    for o in sorted(liste, key=lambda x: x.ab_plan):
        k = o.schluessel()
        if k != g_key and o.ab <= jetzt:
            continue
        daten = fahrt_daten(o, regeln, jetzt) if voll else kompakt(o, regeln, stadt)
        if voll:
            daten["stadt"] = takt.info(stadt, o, regeln)
        daten.update({"schluessel": k, "gewaehlt": k == g_key and not offen,
                      "wird_gewaehlt": k == offen, "empfohlen": k == empfohlen,
                      "fahrplan": k != g_key})
        ergebnis.append(daten)
    return ergebnis


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


def dashboard_daten() -> dict:
    e = config.einstellungen()
    regeln = Regeln.aus_einstellungen(e)
    tz = config.zeitzone()
    jetzt = datetime.now(tz)
    z = zustand.laden()
    heute, morgen = jetzt.date(), jetzt.date() + timedelta(days=1)
    status = z.get("status", {})
    modus = status.get("modus", "normal")
    ue = e["ueberwachung"]

    w = wuensche.laden()
    p_heute = zustand.plan(z, heute)
    aktiv, aktiv_richtung = None, None
    for r in ("hin", "rueck"):
        v = zustand.verbindung(z, heute, r)
        if v and v.an >= jetzt:
            aktiv, aktiv_richtung = fahrt_daten(v, regeln, jetzt), r
            aktiv["stadt"] = takt.info((p_heute.get("stadt") or {}).get(r), v, regeln)
            # Wählen nur, solange die Fahrt noch nicht begonnen hat
            aktiv["optionen"] = optionen(p_heute, heute, r, regeln, jetzt, w, voll=True) if v.ab > jetzt else []
            aktiv["datum"], aktiv["richtung_wert"] = heute.isoformat(), r
            break
    p_morgen = zustand.plan(z, morgen)
    weitere = []    # wischbare Kurzkarten: Heute zurück (während der Hinfahrt), Morgen hin/zurück
    if aktiv_richtung == "hin":
        weitere.append({"titel": "Heute zurück", "datum": heute.isoformat(), "richtung": "rueck",
                        "optionen": optionen(p_heute, heute, "rueck", regeln, jetzt, w)})
    weitere.append({"titel": "Morgen zurück", "datum": morgen.isoformat(), "richtung": "rueck",
                    "optionen": optionen(p_morgen, morgen, "rueck", regeln, jetzt, w)})
    morgen_karte = morgen_daten(p_morgen, morgen, regeln, e)
    morgen_karte.update({"datum": morgen.isoformat(),
                         "optionen": optionen(p_morgen, morgen, "hin", regeln, jetzt, w)})
    if aktiv is None:
        if p_heute and p_heute["tag"]["frei"]:
            leer = "Heute frei"
        elif p_heute:
            leer = "Für heute erledigt"
        else:
            leer = "Noch kein Plan"
    return {
        "datum": f"{stundenplan.WOCHENTAGE_LANG[heute.weekday()]}, {heute.day}. {MONATE[heute.month - 1]}",
        "modus": f"live, alle {ue['intervall_nah_minuten']} min" if modus == "nah"
                 else f"alle {ue['intervall_normal_minuten']} min",
        "stoerung": status.get("fehler", ""),
        "aktualisiert": datetime.fromisoformat(status["takt_um"]).strftime("%H:%M") if status.get("takt_um") else "",
        "aktiv": aktiv,
        "leer": None if aktiv else leer,
        "morgen": morgen_karte,
        "weitere": [x for x in weitere if x["optionen"]],
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
    for o in (p.get("optionen") or {}).get(richtung, []) + ([p[richtung]] if p.get(richtung) else []):
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
    a = wuensche.anpassen(d, an=daten.get("an"), rueck_ab=daten.get("rueck_ab"), campus=daten.get("campus"))
    if a is None:
        return {"ok": True, "text": "Wieder nach Stundenplan, wird neu geplant."}
    hinweis = "" if d <= jetzt.date() + timedelta(days=1) else \
        " Geplant wird ab dem Vortag."   # der Dienst plant nur heute und morgen
    return {"ok": True, "text": f"Gespeichert ({wuensche.beschreibung(a)}), wird neu geplant.{hinweis}"}


POST_ROUTEN = {"/api/waehlen": waehlen, "/api/anpassen": anpassen}


class Handler(BaseHTTPRequestHandler):
    schluessel = ""   # wird in serve() aus der .env gesetzt

    def _senden(self, code: int, typ: str, daten: bytes, extra: Optional[dict] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", typ)
        self.send_header("Cache-Control", "no-store")
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
            self._senden(200, "text/html; charset=utf-8", (STATIC / "index.html").read_bytes())
        elif pfad == "/api/wecker":
            try:
                daten = json.dumps(wecker_daten(), ensure_ascii=False).encode()
                self._senden(200, "application/json; charset=utf-8", daten)
            except Exception:
                log.exception("Wecker-Daten fehlgeschlagen")
                self._senden(500, "application/json", b'{"fehler": "intern"}')
        elif pfad.lstrip("/") in BILDER:
            name = pfad.lstrip("/")
            self._senden(200, BILDER[name], (STATIC / name).read_bytes())
        elif pfad == "/api/status":
            try:
                daten = json.dumps(dashboard_daten(), ensure_ascii=False).encode()
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
