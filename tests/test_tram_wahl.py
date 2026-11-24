"""Tram/Bus zwischen Darmstadt Hbf und Campus selbst wählen ("Nehme ich")."""

from datetime import date, datetime, timedelta

import pytest

from zugbot import config, web, wuensche, zustand
from zugbot.bewertung import Regeln
from zugbot.modell import TZ, Abschnitt, Halt, Teil, Verbindung

D = date(2026, 11, 24)
E = config.einstellungen()
REGELN = Regeln.aus_einstellungen(E)
GELN = Halt(E["haltestellen"]["gelnhausen"]["id"], "Gelnhausen Bahnhof")
DA = Halt(E["haltestellen"]["darmstadt_hbf"]["id"], "Darmstadt Hauptbahnhof")
TRAMSTEIG = DA.id + ":48:48"
CAMPUS = Halt("de-DELFI_de:06411:24730:2:2", "Darmstadt TU-Lichtwiese/Campus")


def t(h, m):
    return datetime(D.year, D.month, D.day, h, m, tzinfo=TZ)


def plan_heute_frueh(zug_an_ist):
    """Wie am 24.11.: RB 68 an Darmstadt Hbf verspätet, dann Tram 2 08:49."""
    rb = Abschnitt(GELN, DA, t(7, 7), t(8, 33), an_ist=zug_an_ist, linie="RB 68", produkt="regional")
    tram = Abschnitt(Halt(TRAMSTEIG, DA.name), CAMPUS, t(8, 49), t(9, 7), linie="Tram 2", produkt="tram")
    v = Verbindung([Teil([rb, tram])], "hin", "lichtwiese", ziel_zeit=t(8, 50))
    stadt = {"ab": None, "fahrten": [
        {"ab": t(8, m).isoformat(), "an": (t(8, m) + timedelta(minutes=18)).isoformat(), "linie": "Tram 2",
         "von": DA.name, "von_id": TRAMSTEIG, "nach": CAMPUS.name, "steig": "3 Ost"}
        for m in (34, 39, 44, 49, 54, 59)]}
    return {"hin": v.als_dict(), "rueck": None, "rueck_offen": True, "optionen": {"hin": [], "rueck": []},
            "stadt": {"hin": stadt}, "wahl": {"hin": v.schluessel()}, "fehler": ""}


@pytest.fixture
def zu(tmp_path, monkeypatch):
    monkeypatch.setattr(wuensche, "PFAD", tmp_path / "wuensche.json")
    monkeypatch.setattr(zustand, "PFAD", tmp_path / "zustand.json")
    monkeypatch.setattr(wuensche, "_heute", lambda: D)
    web._STADT_WUNSCH.clear()
    p = plan_heute_frueh(t(8, 42))
    zustand.speichern({"plaene": {D.isoformat(): p}, "gemeldet": {}, "gesendet": {}})
    return p


def test_trams_trotz_verspaetung_waehlbar(zu):
    jetzt = t(8, 43)
    o = web.optionen(zu, D, "hin", REGELN, jetzt, {})[0]
    st = o["stadt"]
    # Zug kommt 08:42 an: ab da alle Trams, obwohl jede zu spät am Campus ist
    assert [f["ab"] for f in st["fahrten"]] == ["08:44", "08:49", "08:54", "08:59"]
    assert all(f["zu_spaet"] for f in st["fahrten"])
    assert st["fahrten"][0]["knapp"] and not st["fahrten"][1]["knapp"]   # 2 min nach Ankunft
    deine = next(f for f in st["fahrten"] if f["deine"])
    assert deine["ab"] == "08:49" and not deine["waehlbar"]
    assert st["fahrten"][0]["waehlbar"] and st["fahrten"][0]["halt_id"] == TRAMSTEIG
    assert st["nachladbar"]


def test_tram_nehmen_speichert_wunsch(zu):
    jetzt = t(8, 43)
    j = web.zug_nehmen({"datum": D.isoformat(), "richtung": "hin", "pt": "2611240844", "linie": "Tram 2"}, jetzt)
    assert j["ok"] and "Tram 2 08:44" in j["text"]
    z = wuensche.zug(wuensche.laden(), D, "hin")
    assert z["halt_id"] == TRAMSTEIG and z["linie"] == "Tram 2" and z["ab_plan"] == t(8, 44).isoformat()


def test_unbekannte_tram_abgelehnt(zu):
    with pytest.raises(ValueError):
        web.zug_nehmen({"datum": D.isoformat(), "richtung": "hin", "pt": "2611240845", "linie": "Tram 2"}, t(8, 43))


def test_alter_plan_ohne_steig_id_nimmt_hbf(zu):
    for f in zu["stadt"]["hin"]["fahrten"]:
        f.pop("von_id")
    zustand.speichern({"plaene": {D.isoformat(): zu}, "gemeldet": {}, "gesendet": {}})
    web.zug_nehmen({"datum": D.isoformat(), "richtung": "hin", "pt": "2611240854", "linie": "Tram 2"}, t(8, 43))
    assert wuensche.zug(wuensche.laden(), D, "hin")["halt_id"] == DA.id


def test_weitere_trams_laden(zu, monkeypatch):
    aufrufe = []

    class P:
        def stadt_suche(self, richtung, campus, start, fenster, anzahl=12):
            aufrufe.append((richtung, campus, start))
            return [{"ab": t(9, 4).isoformat(), "an": t(9, 22).isoformat(), "linie": "Tram 2",
                     "von": DA.name, "von_id": TRAMSTEIG, "nach": CAMPUS.name, "steig": ""}]

    monkeypatch.setattr(web, "_planer_holen", lambda: P())
    jetzt = t(8, 30)
    web.stadt_laden({"datum": D.isoformat(), "richtung": "hin"}, jetzt)
    assert aufrufe == [("hin", "lichtwiese", t(8, 42))]     # ab der echten Zugankunft
    st = web.optionen(zu, D, "hin", REGELN, jetzt, {})[0]["stadt"]
    assert st["live"] and [f["ab"] for f in st["fahrten"]] == ["09:04"]
    web.zug_nehmen({"datum": D.isoformat(), "richtung": "hin", "pt": "2611240904", "linie": "Tram 2"}, jetzt)
    assert wuensche.zug(wuensche.laden(), D, "hin")["ab_plan"] == t(9, 4).isoformat()
