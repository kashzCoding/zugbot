"""Der Dauerdienst: plant, überwacht, benachrichtigt und liefert das Dashboard aus.

Eine Schleife alle 30 Sekunden prüft, ob etwas fällig ist:
  - Plan für heute/morgen fehlt              -> planen
  - 21:30                                    -> Abendplan für morgen pushen
  - Fahrt heute steht an                     -> alle 30 min aktualisieren,
                                                ab 60 min vor Abfahrt alle 5 min
  - 60 min vor Abfahrt in Gelnhausen         -> Morgen-Push
  - relevante Änderung                       -> sofort pushen, ggf. neu planen
  - eigene Zeiten / gewählte Option (wuensche.py) -> übernehmen, ggf. neu planen
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time, timedelta
from typing import Optional

from . import config, ntfy, stundenplan, takt, texte, ueberwachung, wuensche, zustand
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
        p = self.tag_planen(d, w)
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
