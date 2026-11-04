"""Der Dauerdienst: plant, überwacht, benachrichtigt und liefert das Dashboard aus.

Eine Schleife alle 30 Sekunden prüft, ob etwas fällig ist:
  - Plan für heute/morgen fehlt              -> planen
  - 21:30                                    -> Abendplan für morgen pushen
  - Fahrt heute steht an                     -> alle 30 min aktualisieren,
                                                ab 60 min vor Abfahrt alle 5 min
  - 60 min vor Abfahrt in Gelnhausen         -> Morgen-Push
  - relevante Änderung                       -> sofort pushen, ggf. neu planen
"""

from __future__ import annotations

import logging
import threading
import time as zeitmodul
from datetime import date, datetime, time, timedelta
from typing import Optional

from . import config, ntfy, stundenplan, texte, ueberwachung, zustand
from .bahn import Bahn, BahnFehler
from .bewertung import Regeln
from .modell import Verbindung
from .planer import Planer

log = logging.getLogger(__name__)

TAKT_SEKUNDEN = 30
FRUEHESTE_UEBERWACHUNG_STUNDEN = 6   # vorher lohnt Echtzeit nicht (nachts nichts abfragen)


def tag_als_dict(t: stundenplan.Tag) -> dict:
    return {
        "frei": t.frei, "grund": t.grund, "campus": t.campus, "warnungen": t.warnungen,
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
    def tag_planen(self, d: date) -> dict:
        t = stundenplan.tag(d)
        p = {"tag": tag_als_dict(t), "hin": None, "rueck": None,
             "geplant_um": self.jetzt().isoformat(), "geprueft": {}, "fehler": ""}
        if t.frei:
            return p
        hin = self.planer.hinweg(t).verbindung
        rueck = self.planer.rueckweg(t).verbindung
        p["hin"] = hin.als_dict() if hin else None
        p["rueck"] = rueck.als_dict() if rueck else None
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
            zeitmodul.sleep(TAKT_SEKUNDEN)

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
            for d in (heute, morgen):
                p = z["plaene"].get(d.isoformat())
                # fehlenden Plan anlegen; gescheiterte Planung stündlich wiederholen
                if p is None or (p.get("fehler") and
                                 jetzt - datetime.fromisoformat(p["geplant_um"]) > timedelta(hours=1)):
                    z["plaene"][d.isoformat()] = self.tag_planen(d)

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
                            "db-rest antwortet nicht. Ich versuche es weiter und melde mich, "
                            "sobald es wieder geht.", "hoch")
                status["ausfall_gemeldet"] = True
        status["modus"] = self._modus(z, jetzt)
        status["takt_um"] = jetzt.isoformat()
        zustand.speichern(z)

    def _modus(self, z: dict, jetzt: datetime) -> str:
        for r in ("hin", "rueck"):
            v = zustand.verbindung(z, jetzt.date(), r)
            if v and v.ab - timedelta(minutes=self.ue["nah_ab_minuten_vor_abfahrt"]) <= jetzt <= v.an:
                return "nah"
        return "normal"

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
        t = stundenplan.tag(morgen)
        hin = Verbindung.aus_dict(p["hin"]) if p["hin"] else None
        rueck = Verbindung.aus_dict(p["rueck"]) if p["rueck"] else None
        titel, text = texte.abendplan(t, hin, rueck, self.regeln)
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
