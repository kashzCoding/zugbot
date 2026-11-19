"""Rückfahrt erst mit eigener Zeit; nach "Zeiten anpassen" nur die geänderte Fahrt pushen."""

from datetime import date, datetime, timedelta

import pytest

from zugbot import config, dienst, ntfy, web, wuensche, zustand
from zugbot.bewertung import Regeln
from zugbot.modell import TZ, Abschnitt, Halt, Teil, Verbindung
from zugbot.planer import Ergebnis

HEUTE = date(2026, 11, 4)
PLAN = config.lade_yaml("stundenplan.beispiel.yaml")
GELN = Halt("de-DELFI_de:06435:11058", "Gelnhausen Bahnhof")
LW = Halt("de-DELFI_de:06411:16027", "Darmstadt TU-Lichtwiese Bahnhof")


def unitag():
    for i in range(7):
        d = HEUTE + timedelta(days=i)
        if not wuensche.stundenplan.tag(d, PLAN).frei:
            return d
    raise AssertionError("Beispielplan ohne Unitag")


def verbindung(d, richtung, h, m):
    ab = datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)
    von, nach = (GELN, LW) if richtung == "hin" else (LW, GELN)
    a = Abschnitt(von=von, nach=nach, ab_plan=ab, an_plan=ab + timedelta(minutes=70),
                  linie="RB 82", produkt="regional")
    return Verbindung([Teil([a])], richtung, "lichtwiese", None)


class FalscherPlaner:
    def __init__(self, d):
        self.d, self.aufrufe = d, []

    def hinweg(self, t):
        self.aufrufe.append("hin")
        v = verbindung(self.d, "hin", 7, 0)
        return Ergebnis(v, optionen=[v])

    def rueckweg(self, t):
        self.aufrufe.append("rueck")
        h, m = map(int, t.rueck_ab.strftime("%H:%M").split(":")) if t.rueck_ab else (17, 0)
        v = verbindung(self.d, "rueck", h, m)
        return Ergebnis(v, optionen=[v])

    def stadtfahrten(self, *a):
        return None

    def frueheste_rueckfahrt(self, t):
        return t.rueck_ab


@pytest.fixture
def d(tmp_path, monkeypatch):
    monkeypatch.setattr(wuensche, "PFAD", tmp_path / "wuensche.json")
    monkeypatch.setattr(zustand, "PFAD", tmp_path / "zustand.json")
    monkeypatch.setattr(wuensche, "_heute", lambda: HEUTE)
    monkeypatch.setattr(config, "stundenplan_roh", lambda: PLAN)
    return unitag()


@pytest.fixture
def gesendet(monkeypatch):
    liste = []
    monkeypatch.setattr(ntfy, "senden", lambda titel, text, prio="normal": liste.append((titel, text)))
    return liste


def neuer_dienst(d):
    x = object.__new__(dienst.Dienst)
    x.e = config.einstellungen()
    x.tz = TZ
    x.regeln = Regeln.aus_einstellungen(x.e)
    x.planer = FalscherPlaner(d)
    x.jetzt = lambda: datetime(d.year, d.month, d.day, 6, 0, tzinfo=TZ)
    return x


def test_ohne_eigene_zeit_keine_rueckfahrt(d):
    x = neuer_dienst(d)
    p = x.tag_planen(d, {})
    assert x.planer.aufrufe == ["hin"]
    assert p["rueck"] is None and p["rueck_offen"] and p["fehler"] == ""


def test_mit_zeit_rueckfahrt_und_nur_sie_im_push(d, gesendet):
    x = neuer_dienst(d)
    z = {"plaene": {d.isoformat(): x.tag_planen(d, {})}, "gemeldet": {}, "gesendet": {}}
    hin_vorher = z["plaene"][d.isoformat()]["hin"]
    x.planer.aufrufe.clear()

    wuensche.anpassen(d, rueck_ab="16:00")
    x._angepasst(z, wuensche.laden(), d, x.jetzt())
    p = z["plaene"][d.isoformat()]
    assert x.planer.aufrufe == ["rueck"]           # Hinfahrt nicht neu geplant
    assert p["hin"] == hin_vorher and p["rueck"] and not p["rueck_offen"]
    titel, text = gesendet[-1]
    assert "Rückfahrt" in titel and "Hinfahrt" not in titel
    assert "16:00" in text and "07:00" not in text   # nichts zur Hinfahrt

    # Zeit wieder löschen: Rückfahrt weg, Hinfahrt bleibt
    web.anpassen({"datum": d.isoformat(), "nur_rueck": True, "rueck_ab": ""},
                 datetime(d.year, d.month, d.day, 6, 0, tzinfo=TZ))
    assert wuensche.anpassung(wuensche.laden(), d) is None
    x._angepasst(z, wuensche.laden(), d, x.jetzt())
    p = z["plaene"][d.isoformat()]
    assert p["rueck"] is None and p["rueck_offen"] and p["hin"] == hin_vorher
    assert "keine Zeit" in gesendet[-1][1]


def test_nur_rueck_behaelt_ankunft(d):
    jetzt = datetime(d.year, d.month, d.day, 6, 0, tzinfo=TZ)
    wuensche.anpassen(d, an="10:00")
    wuensche.waehlen(d, "hin", "hin-schluessel")
    web.anpassen({"datum": d.isoformat(), "nur_rueck": True, "rueck_ab": "15:30"}, jetzt)
    w = wuensche.laden()
    assert wuensche.anpassung(w, d)["an"] == "10:00"
    assert wuensche.anpassung(w, d)["rueck_ab"] == "15:30"
    assert wuensche.wahl(w, d, "hin") == "hin-schluessel"   # Hinfahrt-Wahl bleibt
    web.anpassen({"datum": d.isoformat(), "nur_rueck": True, "rueck_ab": ""}, jetzt)
    assert wuensche.anpassung(wuensche.laden(), d) == {"an": "10:00", "rueck_ab": None, "campus": None}


def test_abendplan_nennt_offene_rueckfahrt(d):
    x = neuer_dienst(d)
    p = x.tag_planen(d, {})
    _, text = x._plantext(p, d, {})
    assert "noch keine Zeit" in text


def test_alter_plan_verliert_rueckfahrt_ohne_zeit(d):
    x = neuer_dienst(d)
    x.e = {**x.e, "rueckweg": {**x.e["rueckweg"], "nur_mit_eigener_zeit": False}}
    p = x.tag_planen(d, {})
    p.pop("rueck_offen")
    assert p["rueck"]
    x.e = config.einstellungen()
    x.planer.aufrufe.clear()
    x._rueck_offen_nachtragen(p, {}, d)
    assert p["rueck"] is None and p["rueck_offen"] and p["hin"] and p["fehler"] == ""
    assert x.planer.aufrufe == []


def test_offene_rueckfahrt_einklappbar(d):
    x = neuer_dienst(d)
    p = x.tag_planen(d, {})
    jetzt = x.jetzt()          # vor der Hinfahrt: Hinfahrt ist die Hauptfahrt
    rueck = lambda offen: next(f for f in web.tag_daten(p, d, "Morgen", x.regeln, x.e, jetzt, {}, offen)["fahrten"]
                               if f["richtung"] == "rueck")
    assert rueck(frozenset())["zu"] and rueck(frozenset())["kurz"]["text"] == "noch keine Zeit"
    auf = rueck(frozenset({f"{d.isoformat()}:rueck"}))
    assert not auf["zu"] and auf["aufklappbar"] and auf["rueck_offen"]
