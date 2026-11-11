"""Eigene Zeiten pro Tag und gewählte Verbindung."""

from datetime import date, datetime

import pytest

from zugbot import config, wuensche, zustand
from zugbot.modell import TZ, Abschnitt, Halt, Teil, Verbindung
from zugbot.planer import Planer

HEUTE = date(2026, 11, 4)
DO = date(2026, 11, 5)            # Donnerstag laut Beispielplan ein Unitag
PLAN = config.lade_yaml("stundenplan.beispiel.yaml")


@pytest.fixture(autouse=True)
def datei(tmp_path, monkeypatch):
    monkeypatch.setattr(wuensche, "PFAD", tmp_path / "wuensche.json")
    monkeypatch.setattr(wuensche, "_heute", lambda: HEUTE)


def unitag_im_beispiel():
    for i in range(7):
        d = date(2026, 11, 4 + i)
        if not wuensche.stundenplan.tag(d, PLAN).frei:
            return d
    raise AssertionError("Beispielplan ohne Unitag")


def test_pruefen():
    assert wuensche.pruefen(DO, an="9:30") == {"an": "09:30", "rueck_ab": None, "campus": None}
    for falsch in ({"an": "25:00"}, {"an": "abc"}, {"campus": "mond"}, {"an": "10:00", "rueck_ab": "09:00"}):
        with pytest.raises(ValueError):
            wuensche.pruefen(DO, **falsch)
    with pytest.raises(ValueError):
        wuensche.pruefen(date(2026, 12, 31), an="10:00")    # zu weit voraus


def test_datum_lesen():
    assert wuensche.datum_lesen("morgen", HEUTE) == DO
    assert wuensche.datum_lesen("2026-11-06", HEUTE) == date(2026, 11, 6)
    with pytest.raises(ValueError):
        wuensche.datum_lesen("übermorgen", HEUTE)


def test_eigene_ankunft_zaehlt_wie_beginn():
    d = unitag_im_beispiel()
    wuensche.anpassen(d, an="10:00")
    t = wuensche.tag(d, plan=PLAN)
    assert t.ziel_beginn == datetime(d.year, d.month, d.day, 10, 0, tzinfo=TZ)
    planer = Planer.__new__(Planer)            # nur die Zeitrechnung, ohne Netz
    planer.e = config.einstellungen()
    assert f"{planer.ziel_zeit(t):%H:%M}" == "09:55"


def test_rueckfahrt_und_campus():
    d = unitag_im_beispiel()
    wuensche.anpassen(d, rueck_ab="16:00", campus="stadtmitte")
    t = wuensche.tag(d, plan=PLAN)
    assert t.campus == "stadtmitte" and f"{t.rueck_ab:%H:%M}" == "16:00"
    planer = Planer.__new__(Planer)
    planer.e = config.einstellungen()
    assert f"{planer.frueheste_rueckfahrt(t):%H:%M}" == "16:00"


def test_zuruecksetzen_und_wahl_faellt_weg():
    d = unitag_im_beispiel()
    wuensche.waehlen(d, "hin", "RE 50@0637")
    wuensche.anpassen(d, an="10:00")
    w = wuensche.laden()
    assert wuensche.wahl(w, d, "hin") is None        # neue Zeiten, alte Wahl passt nicht mehr
    wuensche.zuruecksetzen(d)
    assert wuensche.anpassung(wuensche.laden(), d) is None
    assert wuensche.tag(d, plan=PLAN).ziel_beginn is None


def test_freier_tag_braucht_alles():
    frei = date(2026, 11, 7)   # Samstag
    wuensche.anpassen(frei, an="10:00")
    t = wuensche.tag(frei, plan=PLAN)
    assert t.frei and t.warnungen and t.eigene == {"an": "10:00", "rueck_ab": None, "campus": None}
    wuensche.anpassen(frei, an="10:00", rueck_ab="14:00", campus="lichtwiese")
    t = wuensche.tag(frei, plan=PLAN)
    assert not t.frei and t.campus == "lichtwiese" and t.erster.titel == "Eigene Zeit"


def _option(h, m):
    a = Abschnitt(Halt("g", "Gelnhausen"), Halt("f", "Frankfurt"), datetime(2026, 11, 5, h, m, tzinfo=TZ),
                  datetime(2026, 11, 5, h + 1, m, tzinfo=TZ), linie="RE 50", produkt="regionalExpress")
    return Verbindung([Teil([a])], "hin", "lichtwiese")


def test_wahl_anwenden():
    beste, andere = _option(6, 37), _option(6, 7)
    p = {"hin": beste.als_dict(), "optionen": {"hin": [beste.als_dict(), andere.als_dict()]},
         "geprueft": {"hin": "x"}}
    assert zustand.wahl_anwenden(p, "hin", andere.schluessel())
    assert Verbindung.aus_dict(p["hin"]).schluessel() == andere.schluessel()
    assert p["wahl"]["hin"] == andere.schluessel() and "hin" not in p["geprueft"]
    assert not zustand.wahl_anwenden(p, "hin", "gibt-es-nicht")


def test_heim_bis_wird_geprueft_und_beschrieben():
    from datetime import date
    from zugbot import wuensche
    d = date(2026, 11, 11)
    a = wuensche.pruefen(d, heim_bis="17:35", heute=d)
    assert a["heim_bis"] == "17:35" and "zu Hause bis 17:35" in wuensche.beschreibung(a)
    assert "heim_bis" not in wuensche.pruefen(d, an="10:00", heute=d)
    import pytest
    with pytest.raises(ValueError):
        wuensche.pruefen(d, rueck_ab="18:00", heim_bis="17:35", heute=d)
