"""Der Dauerdienst: plant, überwacht, benachrichtigt und liefert das Dashboard aus.

Eine Schleife alle 30 Sekunden prüft, ob etwas fällig ist:
  - Plan für heute/morgen fehlt              -> planen
  - 21:30                                    -> Abendplan für morgen pushen
  - Fahrt heute steht an                     -> alle 30 min aktualisieren,
                                                ab 60 min vor Abfahrt alle 5 min
  - 60 min vor Abfahrt in Gelnhausen         -> Morgen-Push
  - relevante Änderung                       -> sofort pushen, ggf. neu planen
  - eigene Zeiten / gewählte Option (wuensche.py) -> übernehmen, ggf. neu planen
  - "Ich nehme diesen Zug" (wuensche.py)     -> Rest der Fahrt ab diesem Zug planen
  - bei jeder Prüfung einer Fahrt              -> alle Optionen mit DB-Echtzeit auffrischen,
                                                Abfahrtstafel am nächsten Bahnhof
  - alle 30 min                              -> Selbsttest: DNS, Transitous, DB, ntfy
"""

from __future__ import annotations

import logging
import socket
import threading
from datetime import date, datetime, time, timedelta
from typing import Optional

import requests

from . import bewertung, config, ntfy, stundenplan, takt, texte, ueberwachung, wuensche, zustand
from . import planer as planer_mod
from . import tafel as tafel_mod
from .bahn import Bahn, BahnFehler
from .bewertung import Regeln
from .modell import Verbindung
from .planer import Planer

log = logging.getLogger(__name__)

TAKT_SEKUNDEN = 30
SELBSTTEST_MINUTEN = 30
TAFEL_AB_MINUTEN = 90     # Abfahrtstafel ab so lange vor deinem nächsten Zug
FRUEHESTE_UEBERWACHUNG_STUNDEN = 6   # vorher lohnt Echtzeit nicht (nachts nichts abfragen)


def tag_als_dict(t: stundenplan.Tag) -> dict:
    return {
        "frei": t.frei, "grund": t.grund, "campus": t.campus, "warnungen": t.warnungen,
        "eigene": t.eigene,
        "termine": [{"titel": x.titel, "raum": x.raum, "von": x.von.isoformat(),
                     "bis": x.bis.isoformat(), "campus": x.campus} for x in t.termine],
    }


class Dienst:
    def __init__(self):
        self.e = config.einstellungen()
        self.tz = config.zeitzone()
        self.bahn = Bahn(self.e)
        self.planer = Planer(self.bahn, self.e)
        self.regeln = Regeln.aus_einstellungen(self.e)
        self.ue = self.e["ueberwachung"]
        self.naechster_versuch: Optional[datetime] = None

    def jetzt(self) -> datetime:
        return datetime.now(self.tz)

    # ------------------------------------------------------------ Planung
    def tag_planen(self, d: date, w: Optional[dict] = None) -> dict:
        w = wuensche.laden() if w is None else w
        t = wuensche.tag(d, w)
        p = {"tag": tag_als_dict(t), "hin": None, "rueck": None,
             "geplant_um": self.jetzt().isoformat(), "geprueft": {}, "fehler": "",
             "optionen": {"hin": [], "rueck": []}, "stadt": {}, "wahl": {}}
        if t.frei:
            return p
        ergebnisse = {"hin": self.planer.hinweg(t), "rueck": self.planer.rueckweg(t)}
        for r, erg in ergebnisse.items():
            p["optionen"][r] = [o.als_dict() for o in erg.optionen]
            p[r] = erg.verbindung.als_dict() if erg.verbindung else None
            gewaehlt = wuensche.wahl(w, d, r)
            if gewaehlt and not zustand.wahl_anwenden(p, r, gewaehlt):
                log.info("Gewählte Verbindung %s gibt es nicht mehr, nehme die beste", gewaehlt)
        try:   # Takt der letzten Meile ist nur Zusatz: ohne ihn trotzdem planen
            p["stadt"] = {
                "hin": self.planer.stadtfahrten("hin", t.campus, ergebnisse["hin"].optionen),
                "rueck": self.planer.stadtfahrten("rueck", t.campus, ergebnisse["rueck"].optionen,
                                                  self.planer.frueheste_rueckfahrt(t)),
            }
        except BahnFehler as exc:
            log.warning("Tram/Bus-Takt nicht abrufbar: %s", exc)
        hin = Verbindung.aus_dict(p["hin"]) if p["hin"] else None
        rueck = Verbindung.aus_dict(p["rueck"]) if p["rueck"] else None
        if not hin or not rueck:
            p["fehler"] = "Keine passende " + ("Hinfahrt" if not hin else "Rückfahrt") + " gefunden."
        log.info("Plan %s: hin %s, rück %s", d, hin and hin.schluessel(), rueck and rueck.schluessel())
        return p

    # ------------------------------------------------------------ Schleife
    def laufen(self) -> None:
        log.info("Zug-Bot gestartet")
        while True:
            try:
                self.takt()
            except Exception:   # der Dienst soll bei einem Fehler weiterlaufen
                log.exception("Unerwarteter Fehler im Takt")
            # 30 s warten, außer das Dashboard meldet einen neuen Wunsch
            wuensche.GEAENDERT.wait(TAKT_SEKUNDEN)
            wuensche.GEAENDERT.clear()

    def takt(self) -> None:
        jetzt = self.jetzt()
        if self.naechster_versuch and jetzt < self.naechster_versuch:
            return
        z = zustand.laden()
        z.setdefault("plaene", {})
        z.setdefault("gemeldet", {})
        z.setdefault("gesendet", {})
        status = z.setdefault("status", {})
        heute, morgen = jetzt.date(), jetzt.date() + timedelta(days=1)
        zustand.aufraeumen(z, heute)

        try:
            w = wuensche.laden()
            for d in (heute, morgen):
                p = z["plaene"].get(d.isoformat())
                # fehlenden Plan anlegen; gescheiterte Planung stündlich wiederholen
                if p is None or (p.get("fehler") and
                                 jetzt - datetime.fromisoformat(p["geplant_um"]) > timedelta(hours=1)):
                    z["plaene"][d.isoformat()] = self.tag_planen(d, w)
                elif p["tag"].get("eigene") != wuensche.anpassung(w, d):
                    self._angepasst(z, w, d, jetzt)
                else:
                    self._wahl_uebernehmen(z, w, d)
                    self._zug_uebernehmen(z, w, d, jetzt)

            self._abendplan(z, jetzt, morgen)
            for richtung in ("hin", "rueck"):
                self._ueberwachen(z, jetzt, heute, richtung)

            if status.get("ausfall_gemeldet"):
                ntfy.senden("Bahndaten wieder da", "Die Verbindungsdaten sind wieder erreichbar.", "niedrig")
            status.update({"ausfall_gemeldet": False, "fehler": "", "ok_um": jetzt.isoformat()})
            self.naechster_versuch = None
        except BahnFehler as exc:
            status["fehler"] = str(exc)[:200]
            # Bei Ausfall nicht alle 30 s hämmern: in 5 Minuten wieder versuchen
            self.naechster_versuch = jetzt + timedelta(minutes=5)
            if self.bahn.fehler_in_folge >= self.e["bahn"]["ausfall_meldung_nach"] \
                    and not status.get("ausfall_gemeldet"):
                ntfy.senden("Bahndaten nicht erreichbar",
                            "Transitous antwortet nicht. Ich versuche es weiter und melde mich, "
                            "sobald es wieder geht.", "hoch")
                status["ausfall_gemeldet"] = True
        self._selbsttest(status, jetzt)
        status["modus"] = self._modus(z, jetzt)
        status["takt_um"] = jetzt.isoformat()
        zustand.speichern(z)

    def _modus(self, z: dict, jetzt: datetime) -> str:
        for r in ("hin", "rueck"):
            v = zustand.verbindung(z, jetzt.date(), r)
            if v and v.ab - timedelta(minutes=self.ue["nah_ab_minuten_vor_abfahrt"]) <= jetzt <= v.an:
                return "nah"
        return "normal"

    # ------------------------------------------------------------ Wünsche
    def _angepasst(self, z: dict, w: dict, d: date, jetzt: datetime) -> None:
        """Eigene Zeiten geändert: Tag neu planen."""
        log.info("Eigene Zeiten für %s: %s", d, wuensche.anpassung(w, d))
        alt = z["plaene"].get(d.isoformat()) or {}
        p = self.tag_planen(d, w)
        # Schon gefahrene Hinfahrt behalten: die neue Planung kennt nur den Fahrplan
        if alt.get("hin") and Verbindung.aus_dict(alt["hin"]).an < jetzt:
            p["hin"] = alt["hin"]
            p["optionen"]["hin"] = (alt.get("optionen") or {}).get("hin", [])
            if p.get("rueck"):
                p["fehler"] = ""
        z["plaene"][d.isoformat()] = p
        for r in ("hin", "rueck"):
            z["gemeldet"].pop(f"{d.isoformat()}:{r}", None)
        # Push nur, wenn der Abendplan sonst nichts mehr davon erzählt:
        # für heute, oder für morgen, nachdem der Abendplan schon raus ist
        if d == jetzt.date() or z["gesendet"].get(f"{d.isoformat()}:abend"):
            titel, text = self._plantext(p, d, w)
            ntfy.senden(f"Angepasst: {titel}", text, "normal")

    def _wahl_uebernehmen(self, z: dict, w: dict, d: date) -> None:
        p = z["plaene"][d.isoformat()]
        for r in ("hin", "rueck"):
            gewaehlt = wuensche.wahl(w, d, r)
            if gewaehlt and gewaehlt != p.get("wahl", {}).get(r):
                if zustand.wahl_anwenden(p, r, gewaehlt):
                    log.info("Gewählt für %s %s: %s", d, r, gewaehlt)
                    z["gemeldet"].pop(f"{d.isoformat()}:{r}", None)

    def _zug_uebernehmen(self, z: dict, w: dict, d: date, jetzt: datetime) -> None:
        """"Ich nehme diesen Zug": Rest der Fahrt ab diesem Zug planen und überwachen."""
        p = z["plaene"][d.isoformat()]
        for r in ("hin", "rueck"):
            zug = wuensche.zug(w, d, r)
            if not zug or zug == (p.get("zug") or {}).get(r) or not p.get(r):
                continue
            p.setdefault("zug", {})[r] = zug
            ab = datetime.fromisoformat(zug["ab_plan"])
            if ab < jetzt - timedelta(minutes=30):
                continue      # alter Wunsch (z. B. nach einer Neuplanung): der Zug ist längst weg
            neu = self.planer.ab_zug(Verbindung.aus_dict(p[r]), zug["halt_id"], zug["linie"], ab, jetzt)
            if neu is None:
                p.setdefault("zug_fehler", {})[r] = f"Mit {zug['linie']} {ab:%H:%M} keine Weiterfahrt gefunden, der bisherige Plan bleibt."
                ntfy.senden("Keine Verbindung gefunden",
                            f"Mit {zug['linie']} {ab:%H:%M} finde ich keine Weiterfahrt. "
                            "Der bisherige Plan bleibt.", "hoch")
                continue
            log.info("Zug genommen %s %s: %s", d, r, neu.schluessel())
            p.get("zug_fehler", {}).pop(r, None)
            p[r] = neu.als_dict()
            p.setdefault("wahl", {})[r] = neu.schluessel()
            p.get("geprueft", {}).pop(r, None)
            z["gemeldet"].pop(f"{d.isoformat()}:{r}", None)
            klappt = r == "rueck" or not neu.ziel_zeit or neu.an <= neu.ziel_zeit
            ntfy.senden("Neuer Plan" if klappt else "Neuer Plan, aber zu spät",
                        texte.kurzfassung(neu, self.regeln), "normal" if klappt else "hoch")

    def _optionen_live(self, p: dict, richtung: str, gewaehlt: Verbindung, jetzt: datetime) -> None:
        """Alle Optionen mit DB-Echtzeit auffrischen und Kombinationen "anderer Zug,
        dann weiter wie geplant" ergänzen. Keine Anfragen an Transitous; die
        DB-Tafeln sind zwischengespeichert."""
        optionen = []
        for o in (p.get("optionen") or {}).get(richtung, []):
            v = Verbindung.aus_dict(o)
            self.planer.gleise.ergaenzen(v)
            optionen.append(v)
        p.setdefault("optionen", {})[richtung] = [v.als_dict() for v in optionen]
        bekannt = {v.schluessel() for v in optionen} | {gewaehlt.schluessel()}
        zusatz = [v for v in planer_mod.mit_anschluss(optionen, gewaehlt, self.regeln, jetzt)
                  if v.schluessel() not in bekannt]
        p.setdefault("zusatz", {})[richtung] = [v.als_dict() for v in zusatz]
        p.setdefault("tafel", {})[richtung] = self._tafel(gewaehlt, jetzt)

    def _tafel(self, v: Verbindung, jetzt: datetime) -> Optional[dict]:
        return tafel_mod.bauen(v, jetzt, self.planer.gleise, self.regeln, self.e)

    def _selbsttest(self, status: dict, jetzt: datetime) -> None:
        """Prüft alle 30 min, ob DNS und die Dienste erreichbar sind. Das Ergebnis
        zeigt das Dashboard; es läuft über Funnel und geht auch ohne DNS auf dem Pi."""
        letzter = status.get("selbsttest", {}).get("um")
        if letzter and jetzt - datetime.fromisoformat(letzter) < timedelta(minutes=SELBSTTEST_MINUTEN):
            return
        env = config.env()
        ziele = {"Transitous": env.transitous_url, "ntfy": env.ntfy_server}
        if self.planer.gleise.aktiv:
            ziele["DB-API"] = "https://apis.deutschebahn.com"
        fehler = []
        for name, url in ziele.items():
            host = url.split("//", 1)[-1].split("/", 1)[0]
            try:
                socket.getaddrinfo(host, 443)
                requests.head(url, timeout=10, headers={"User-Agent": env.user_agent})
            except socket.gaierror:
                fehler.append(f"{name}: Name nicht auflösbar (DNS)")
            except requests.RequestException as exc:
                fehler.append(f"{name}: nicht erreichbar ({type(exc).__name__})")
        if fehler and not status.get("selbsttest", {}).get("fehler"):
            log.warning("Selbsttest: %s", "; ".join(fehler))
            ntfy.senden("Zug-Bot: Verbindungsproblem", "\n".join(fehler), "hoch")   # klappt, falls ntfy geht
        status["selbsttest"] = {"um": jetzt.isoformat(), "fehler": fehler}

    def _plantext(self, p: dict, d: date, w: Optional[dict] = None) -> tuple[str, str]:
        t = wuensche.tag(d, w)
        hin = Verbindung.aus_dict(p["hin"]) if p["hin"] else None
        rueck = Verbindung.aus_dict(p["rueck"]) if p["rueck"] else None
        stadt = takt.info(p.get("stadt", {}).get("hin"), hin, self.regeln) if hin else None
        wann = "Heute" if d == self.jetzt().date() else "Morgen"
        return texte.abendplan(t, hin, rueck, self.regeln, wann=wann,
                               takt=f"{stadt['titel']}: {stadt['text']}" if stadt else "")

    # ------------------------------------------------------------ Abendplan
    def _abendplan(self, z: dict, jetzt: datetime, morgen: date) -> None:
        h, m = map(int, self.e["benachrichtigung"]["abendplan_uhrzeit"].split(":"))
        schluessel = f"{morgen.isoformat()}:abend"
        if jetzt.time() < time(h, m) or z["gesendet"].get(schluessel):
            return
        # frisch planen, damit Fahrplanänderungen vom Tag berücksichtigt sind
        p = self.tag_planen(morgen)
        z["plaene"][morgen.isoformat()] = p
        z["gemeldet"].pop(f"{morgen.isoformat()}:hin", None)
        z["gemeldet"].pop(f"{morgen.isoformat()}:rueck", None)
        titel, text = self._plantext(p, morgen)
        ntfy.senden(titel, text, "normal" if not p["fehler"] else "hoch")
        z["gesendet"][schluessel] = jetzt.isoformat()

    # ------------------------------------------------------------ Überwachung
    def _ueberwachen(self, z: dict, jetzt: datetime, d: date, richtung: str) -> None:
        p = z["plaene"].get(d.isoformat())
        v = zustand.verbindung(z, d, richtung)
        if not p or not v or v.an < jetzt - timedelta(minutes=5):
            return
        if jetzt < v.ab_plan - timedelta(hours=FRUEHESTE_UEBERWACHUNG_STUNDEN):
            return

        nah_ab = v.ab - timedelta(minutes=self.ue["nah_ab_minuten_vor_abfahrt"])
        intervall = self.ue["intervall_nah_minuten"] if jetzt >= nah_ab else self.ue["intervall_normal_minuten"]
        push_schluessel = f"{d.isoformat()}:morgen"
        push_faellig = (richtung == "hin" and not z["gesendet"].get(push_schluessel)
                        and jetzt >= v.ab_plan - timedelta(
                            minutes=self.e["benachrichtigung"]["morgen_push_minuten_vor_abfahrt"]))
        zuletzt_geprueft = p["geprueft"].get(richtung)
        if zuletzt_geprueft and not push_faellig and \
                jetzt - datetime.fromisoformat(zuletzt_geprueft) < timedelta(minutes=intervall) - timedelta(seconds=15):
            return

        v_neu = ueberwachung.aktualisiere(self.bahn, v, self.planer.gleise)
        mkey = f"{d.isoformat()}:{richtung}"
        zuletzt = z["gemeldet"].get(mkey, {})
        meldungen = ueberwachung.lage(v_neu, self.regeln, jetzt, zuletzt,
                                      self.ue["abfahrt_verspaetung_melden_ab_minuten"])

        if ueberwachung.braucht_neuplanung(meldungen):
            neu = self.planer.neu_planen(v_neu, jetzt)
            if ueberwachung.ist_besser(neu, v_neu, self.regeln, jetzt):
                klappt = richtung == "rueck" or not neu.ziel_zeit or neu.an <= neu.ziel_zeit
                gruende = "\n".join(m.text for m in meldungen if m.neu_planen)
                ntfy.senden("Neue Route" if klappt else "Verbindung klappt nicht mehr",
                            f"{gruende}\n\n{texte.kurzfassung(neu, self.regeln)}",
                            "hoch" if klappt else "dringend")
                meldungen = [m for m in meldungen if not m.neu_planen]
                v_neu = neu
            elif any(m.dringend for m in meldungen):
                meldungen.append(ueberwachung.Meldung("info", "Keine bessere Verbindung gefunden."))

        if meldungen:
            dringend = any(m.dringend for m in meldungen)
            haupt = next((m for m in meldungen if m.dringend), meldungen[0])
            titel = {"ausfall": "Ausfall", "umstieg": "Anschluss gefährdet", "gleis": "Gleiswechsel",
                     "zu_spaet": "Verspätete Ankunft", "abfahrt": "Spätere Abfahrt",
                     "entwarnung": "Entwarnung"}.get(haupt.art, "Hinweis")
            prio = "dringend" if dringend else ("niedrig" if haupt.art == "entwarnung" else "normal")
            ntfy.senden(titel, "\n".join(m.text for m in meldungen), prio)

        z["gemeldet"][mkey] = ueberwachung.zustand_schluessel(v_neu, self.regeln, jetzt)
        p[richtung] = v_neu.als_dict()
        self._optionen_live(p, richtung, v_neu, jetzt)
        p["geprueft"][richtung] = jetzt.isoformat()

        if push_faellig:
            titel, text = texte.morgenstand(v_neu, self.regeln)
            ntfy.senden(titel, text, "normal")
            z["gesendet"][push_schluessel] = jetzt.isoformat()


def starten() -> None:
    from . import web
    dienst = Dienst()
    threading.Thread(target=web.serve, daemon=True, name="web").start()
    dienst.laufen()
