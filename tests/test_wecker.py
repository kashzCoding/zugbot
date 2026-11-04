"""/api/wecker: Weckzeit = Abfahrt Gelnhausen minus Vorlauf, oder frei."""

from datetime import datetime

import pytest

from zugbot import config, web, zustand
from zugbot.modell import TZ, Abschnitt, Halt, Teil, Verbindung

JETZT = datetime(2026, 11, 4, 21, 0, tzinfo=TZ)      # Mittwochabend
MORGEN = "2026-11-05"


def plan_mit_fahrt(ab_ist=None):
    erste = Abschnitt(Halt("x", "Gelnhausen Bahnhof"), Halt("y", "Frankfurt (Main) Hauptbahnhof"),
                      ab_plan=datetime(2026, 11, 5, 6, 37, tzinfo=TZ), an_plan=datetime(2026, 11, 5, 7, 17, tzinfo=TZ),
                      ab_ist=ab_ist, linie="RE 50", produkt="regionalExpress", gleis_ab="2")
    v = Verbindung([Teil([erste])], "hin", "lichtwiese")
    return {"tag": {"frei": False, "grund": "", "campus": "lichtwiese", "warnungen": [],
                    "termine": [{"titel": "GdD Hörsaalübung", "raum": "L4|02 202",
                                 "von": "2026-11-05T08:00:00+01:00", "bis": "2026-11-05T09:40:00+01:00",
                                 "campus": "lichtwiese"}]},
            "hin": v.als_dict(), "rueck": None, "geplant_um": "2026-11-04T21:30:00+01:00",
            "geprueft": {}, "fehler": ""}


@pytest.fixture
def zustand_datei(tmp_path, monkeypatch):
    monkeypatch.setattr(zustand, "PFAD", tmp_path / "zustand.json")
    return lambda plan: zustand.speichern({"plaene": {MORGEN: plan}} if plan else {})


def test_weckzeit_40_minuten_vor_abfahrt(zustand_datei):
    zustand_datei(plan_mit_fahrt())
    w = web.wecker_daten(JETZT)
    assert w["status"] == "ok" and w["frei"] is False
    assert (w["abfahrt"], w["weckzeit"]) == ("06:37", "05:57")
    assert w["weckzeit_iso"] == "2026-11-05T05:57:00+01:00"
    assert w["gleis"] == "2" and w["erster_termin"]["raum"] == "L4|02 202"


def test_verspaetete_abfahrt_verschiebt_weckzeit(zustand_datei):
    zustand_datei(plan_mit_fahrt(ab_ist=datetime(2026, 11, 5, 6, 45, tzinfo=TZ)))
    assert web.wecker_daten(JETZT)["weckzeit"] == "06:05"


def test_vorlauf_aus_einstellungen(zustand_datei, monkeypatch):
    zustand_datei(plan_mit_fahrt())
    e = config.einstellungen()
    e["wecker"] = {"vorlauf_minuten": 60}
    monkeypatch.setattr(config, "einstellungen", lambda: e)
    w = web.wecker_daten(JETZT)
    assert (w["weckzeit"], w["vorlauf_minuten"]) == ("05:37", 60)


def test_frei(zustand_datei):
    zustand_datei({"tag": {"frei": True, "grund": "Weihnachtspause", "campus": None,
                           "termine": [], "warnungen": []}, "hin": None, "rueck": None})
    w = web.wecker_daten(JETZT)
    assert w["status"] == "frei" and w["frei"] is True and w["weckzeit"] is None
    assert w["grund"] == "Weihnachtspause"


def test_noch_kein_plan(zustand_datei):
    zustand_datei(None)
    w = web.wecker_daten(JETZT)
    assert w["status"] == "noch_kein_plan" and w["weckzeit"] is None
