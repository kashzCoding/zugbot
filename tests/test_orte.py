"""Eberstadt als wählbarer Ort und "Los ab" für die Hinfahrt."""

from datetime import date, datetime, timedelta

import pytest

from zugbot import config, wuensche
from zugbot.bewertung import Regeln, pruefe
from zugbot.modell import TZ, Abschnitt, Halt, Teil, Verbindung
from zugbot.planer import Planer

HEUTE = date(2026, 11, 23)
D = date(2026, 11, 24)
GELN = Halt("de-DELFI_de:06435:11058", "Gelnhausen Bahnhof")
FFM = Halt("de-DELFI_de:06412:10_G", "Frankfurt (Main) Hauptbahnhof")
DA = Halt("de-DELFI_de:06411:4734", "Darmstadt Hauptbahnhof")
LW = Halt("de-DELFI_de:06411:24730:2:2", "Darmstadt TU-Lichtwiese/Campus")
EBER = Halt("de-DELFI_de:06411:15921:1:1", "Darmstadt-Eberstadt Wartehalle")


def t(h, m):
    return datetime(D.year, D.month, D.day, h, m, tzinfo=TZ)


def fahrt(von, nach, ab, an, linie="Tram 1", produkt="tram"):
    return Abschnitt(von=von, nach=nach, ab_plan=ab, an_plan=an, linie=linie, produkt=produkt)


@pytest.fixture(autouse=True)
def datei(tmp_path, monkeypatch):
    monkeypatch.setattr(wuensche, "PFAD", tmp_path / "wuensche.json")
    monkeypatch.setattr(wuensche, "_heute", lambda: HEUTE)


@pytest.fixture(scope="module")
def regeln():
    return Regeln.aus_einstellungen(config.einstellungen())


def test_frankfurt_nur_auf_dem_weg_nach_gelnhausen(regeln):
    stadt = Verbindung([Teil([fahrt(LW, EBER, t(16, 2), t(16, 30))])], "rueck", "lichtwiese", ziel="eberstadt")
    assert pruefe(stadt, regeln) == []
    heim = Verbindung([Teil([fahrt(EBER, DA, t(20, 0), t(20, 15)),
                             fahrt(DA, GELN, t(20, 25), t(21, 40), "RB 61", "regional")])], "rueck", "lichtwiese",
                      start="eberstadt")
    assert any("Frankfurt" in g for g in pruefe(heim, regeln))


def test_orte_pruefen_und_beschreiben():
    a = wuensche.pruefen(D, rueck_nach="eberstadt", heute=HEUTE)
    assert a["rueck_nach"] == "eberstadt" and "Eberstadt" in wuensche.beschreibung(a)
    # Standard wird nicht gespeichert, alte Einträge bleiben gleich
    assert "hin_von" not in wuensche.pruefen(D, hin_von="gelnhausen", heute=HEUTE)
    for falsch in ({"hin_von": "mond"}, {"rueck_von": "eberstadt", "rueck_nach": "eberstadt"},
                   {"an": "10:00", "los_ab": "08:00"}, {"hin_nach": "gelnhausen"}):
        with pytest.raises(ValueError):
            wuensche.pruefen(D, heute=HEUTE, **falsch)


def test_tag_kennt_orte_und_los_ab():
    wuensche.anpassen(D, los_ab="07:15", hin_von="eberstadt", rueck_nach="eberstadt")
    tag = wuensche.tag(D, plan=config.lade_yaml("stundenplan.beispiel.yaml"))
    assert tag.los_ab == t(7, 15) and tag.hin_von == "eberstadt" and tag.rueck_nach == "eberstadt"
    assert Planer.strecke(tag, "hin") == ("eberstadt", "campus")
    assert Planer.strecke(tag, "rueck") == ("campus", "eberstadt")


class FalscheBahn:
    def __init__(self):
        self.anfragen = []

    def verbindungen(self, von, nach, **kw):
        self.anfragen.append((von, nach, kw))
        return []


def test_planer_sucht_gewaehlte_orte_und_los_ab(monkeypatch):
    e = config.einstellungen()
    pl = Planer(FalscheBahn(), e)
    gefunden = Verbindung([Teil([fahrt(EBER, LW, t(7, 20), t(7, 50))])], "hin", "lichtwiese")
    monkeypatch.setattr(pl, "_suche", lambda von, nach, **kw: pl.bahn.anfragen.append((von, nach, kw)) or
                        ([gefunden.teile[0]] if kw.get("abfahrt") else []))
    wuensche.anpassen(D, los_ab="07:15", hin_von="eberstadt")
    tag = wuensche.tag(D)
    if tag.frei:
        pytest.skip("Tag laut Stundenplan frei")
    erg = pl.hinweg(tag)
    anfragen = pl.bahn.anfragen
    assert {a[0] for a in anfragen} == {pl.hid("eberstadt_wartehalle")}
    assert all(a[2]["abfahrt"] == t(7, 15) and a[2].get("ueber") is None for a in anfragen)
    assert erg.verbindung and erg.verbindung.start == "eberstadt" and erg.verbindung.ziel_zeit is None


def test_verbindung_merkt_sich_orte():
    v = Verbindung([Teil([fahrt(LW, EBER, t(16, 2), t(16, 30))])], "rueck", "lichtwiese", ziel="eberstadt")
    w = Verbindung.aus_dict(v.als_dict())
    assert w.ziel_ort == "eberstadt" and w.start_ort == "campus"
    assert w.wie(w.teile).ziel == "eberstadt"
