"""Dashboard: kleine Webseite + /api/status (JSON), ausgeliefert vom Pi.

Bewusst nur mit der Python-Standardbibliothek (http.server), damit auf dem
Pi Zero kein zusätzliches Framework RAM braucht. Nur lesend, keine Eingaben.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from . import bewertung, config, stundenplan, texte, zustand
from .bewertung import Regeln
from .modell import Verbindung

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
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
    daten = {"wochentag": wt, "frei": False, "campus": texte.campus_name(p["tag"]["campus"]),
             "termin": f"{t_von:%H:%M} {termin['titel']}, {termin['raum']}"}
    if v is None:
        daten["fehlt"] = p.get("fehler") or "Keine Verbindung gefunden."
        return daten
    erste = v.fahrten[0]
    ffm = next((u for u in bewertung.umstiege(v, regeln) if "frankfurt" in u.ort.lower()), None)
    push = v.ab_plan - timedelta(minutes=e["benachrichtigung"]["morgen_push_minuten_vor_abfahrt"])
    daten.update({
        "ab": f"{erste.ab:%H:%M}", "gleis": _gleis_label(erste),
        "an": f"an {v.an:%H:%M}", "dauer": texte.dauer(v.dauer_min),
        "umstieg": f"Umstieg {texte.kurzname(ffm.ort)} {ffm.minuten} min" if ffm else
                   f"{v.umstiege} Umstieg(e)",
        "push": f"Morgen-Push um {push:%H:%M}",
    })
    return daten


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

    aktiv = None
    for r in ("hin", "rueck"):
        v = zustand.verbindung(z, heute, r)
        if v and v.an >= jetzt:
            aktiv = fahrt_daten(v, regeln, jetzt)
            break
    p_heute = zustand.plan(z, heute)
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
        "morgen": morgen_daten(zustand.plan(z, morgen), morgen, regeln, e),
    }


class Handler(BaseHTTPRequestHandler):
    def _senden(self, code: int, typ: str, daten: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", typ)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(daten)))
        self.end_headers()
        self.wfile.write(daten)

    def do_GET(self):
        pfad = self.path.split("?")[0]
        if pfad in ("/", "/index.html"):
            self._senden(200, "text/html; charset=utf-8", (STATIC / "index.html").read_bytes())
        elif pfad == "/api/status":
            try:
                daten = json.dumps(dashboard_daten(), ensure_ascii=False).encode()
                self._senden(200, "application/json; charset=utf-8", daten)
            except Exception:
                log.exception("Dashboard-Daten fehlgeschlagen")
                self._senden(500, "application/json", b'{"fehler": "intern"}')
        else:
            self._senden(404, "text/plain; charset=utf-8", "nicht gefunden".encode())

    def log_message(self, fmt, *args):   # Zugriffe nicht ins Log schreiben
        pass


def serve() -> None:
    env = config.env()
    port = config.einstellungen()["web"]["port"]
    log.info("Dashboard auf http://%s:%s", env.web_host, port)
    ThreadingHTTPServer((env.web_host, port), Handler).serve_forever()
