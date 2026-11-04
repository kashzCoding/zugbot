"""Tests der harten Regeln und der Bewertung mit erfundenen Verbindungen.

Ausführen: .venv/bin/pytest
"""

from datetime import datetime, timedelta

import pytest

from zugbot import config
from zugbot.bewertung import Regeln, pruefe, punkte_hin, umstiege
from zugbot.modell import TZ, Abschnitt, Halt, Teil, Verbindung
from zugbot.ueberwachung import lage, zustand_schluessel

GELN = Halt("de-DELFI_de:06435:11058", "Gelnhausen Bahnhof")
FFM = Halt("de-DELFI_de:06412:10_G", "Frankfurt (Main) Hauptbahnhof")
SUED = Halt("de-DELFI_de:06412:912_G", "Frankfurt (Main) Südbahnhof")
DA = Halt("de-DELFI_de:06411:4734", "Darmstadt Hauptbahnhof")
DA_TRAM = Halt("de-DELFI_de:06411:4734:48:48", "Darmstadt Hauptbahnhof")
LW = Halt("de-DELFI_de:06411:16027", "Darmstadt TU-Lichtwiese Bahnhof")
SCHLOSS = Halt("de-DELFI_de:06411:16016:1:1", "Darmstadt Schloss")
BABENHAUSEN = Halt("de-DELFI_de:06432:1", "Babenhausen (Hessen) Bahnhof")
HANAU = Halt("de-DELFI_de:06435:4503", "Hanau Hauptbahnhof")


def t(h, m, tag=9):
    return datetime(2026, 11, tag, h, m, tzinfo=TZ)


def fahrt(von, nach, ab, an, linie="RE 50", produkt="regionalExpress", zwischen=(), **kw):
    return Abschnitt(von=von, nach=nach, ab_plan=ab, an_plan=an, linie=linie, produkt=produkt,
                     zwischenhalte=list(zwischen), **kw)


def verbindung(*abschnitte, richtung="hin"):
    return Verbindung([Teil(list(abschnitte))], richtung, "lichtwiese", t(8, 50))


@pytest.fixture(scope="module")
def regeln():
    return Regeln.aus_einstellungen(config.einstellungen())


def test_normale_verbindung_ok(regeln):
    v = verbindung(fahrt(GELN, FFM, t(6, 37), t(7, 17), zwischen=[HANAU]),
                   fahrt(FFM, LW, t(7, 26), t(7, 50), "RB 82", "regional"))
    assert pruefe(v, regeln) == []


def test_ice_wird_verworfen(regeln):
    v = verbindung(fahrt(GELN, FFM, t(6, 37), t(7, 17)),
                   fahrt(FFM, LW, t(7, 26), t(7, 50), "ICE 13", "nationalExpress"))
    assert any("Deutschlandticket" in g for g in pruefe(v, regeln))


def test_babenhausen_auf_gefahrenem_abschnitt_verworfen(regeln):
    v = verbindung(fahrt(GELN, HANAU, t(6, 0), t(6, 20)),
                   fahrt(HANAU, DA, t(6, 30), t(7, 20), "RB 61", "regional", zwischen=[BABENHAUSEN]))
    gruende = pruefe(v, regeln)
    assert any("Babenhausen" in g for g in gruende)
    assert any("Frankfurt" in g for g in gruende)


def test_babenhausen_nach_dem_aussteigen_egal(regeln):
    # Die RB fährt nach der Lichtwiese weiter Richtung Groß-Umstadt. Solange
    # das nicht in den gefahrenen Halten steht, ist alles gut.
    v = verbindung(fahrt(GELN, FFM, t(6, 37), t(7, 17)),
                   fahrt(FFM, LW, t(7, 26), t(7, 50), "RB 82", "regional", richtung="Groß-Umstadt Wiebelsbach"))
    assert pruefe(v, regeln) == []


def test_darmstadt_hbf_braucht_7_minuten(regeln):
    zug = fahrt(SUED, DA, t(7, 0), t(7, 30), "S6", "suburban")
    v6 = verbindung(fahrt(GELN, SUED, t(6, 20), t(6, 50)), zug,
                    fahrt(DA_TRAM, SCHLOSS, t(7, 36), t(7, 44), "Tram 2", "tram"))
    v7 = verbindung(fahrt(GELN, SUED, t(6, 20), t(6, 50)), zug,
                    fahrt(DA_TRAM, SCHLOSS, t(7, 37), t(7, 45), "Tram 2", "tram"))
    assert any("nur 6 min" in g for g in pruefe(v6, regeln))
    assert pruefe(v7, regeln) == []
    assert umstiege(v7, regeln)[1].minimum == 7


def test_sonst_5_minuten_reichen(regeln):
    v = verbindung(fahrt(GELN, FFM, t(6, 37), t(7, 17)),
                   fahrt(FFM, LW, t(7, 22), t(7, 50), "RB 82", "regional"))
    assert pruefe(v, regeln) == []


def test_bewertung_spaeter_los_gewinnt_auch_ueber_mitternacht(regeln):
    frueh = verbindung(fahrt(GELN, FFM, t(23, 59, tag=8), t(0, 38)),
                       fahrt(FFM, LW, t(1, 0), t(1, 30), "RB 82", "regional"))
    spaet = verbindung(fahrt(GELN, FFM, t(6, 37), t(7, 17)),
                       fahrt(FFM, LW, t(7, 26), t(7, 50), "RB 82", "regional"))
    assert punkte_hin(spaet, regeln) > punkte_hin(frueh, regeln)


def test_meldung_nur_bei_folgen(regeln):
    jetzt = t(6, 0)
    erste = fahrt(GELN, FFM, t(6, 37), t(7, 17), ab_ist=t(6, 39), an_ist=t(7, 19))
    v = verbindung(erste, fahrt(FFM, LW, t(7, 26), t(7, 50), "RB 82", "regional"))
    # +2 min, Umstieg bleibt 7 min: keine Meldung
    assert lage(v, regeln, jetzt, {}, abfahrt_schwelle=5) == []
    # +5 min, Umstieg nur noch 4 min: Meldung und Neuplanung
    erste.ab_ist, erste.an_ist = t(6, 42), t(7, 22)
    meldungen = lage(v, regeln, jetzt, {}, abfahrt_schwelle=5)
    assert any(m.art == "umstieg" and m.neu_planen for m in meldungen)
    # dieselbe Lage beim nächsten Abruf: nicht nochmal melden
    zuletzt = zustand_schluessel(v, regeln, jetzt)
    assert [m for m in lage(v, regeln, jetzt, zuletzt, 5) if m.art == "umstieg"] == []


def test_gleiswechsel_wird_gemeldet(regeln):
    v = verbindung(fahrt(GELN, FFM, t(6, 37), t(7, 17), gleis_ab="2", gleis_ab_plan="1"),
                   fahrt(FFM, LW, t(7, 26), t(7, 50), "RB 82", "regional"))
    meldungen = lage(v, regeln, t(6, 0), {}, 5)
    assert [m.art for m in meldungen] == ["gleis"]
    assert "Gleis 2" in meldungen[0].text


def test_speichern_und_laden():
    v = verbindung(fahrt(GELN, FFM, t(6, 37), t(7, 17), ab_ist=t(6, 40)))
    zurueck = Verbindung.aus_dict(v.als_dict())
    assert zurueck.fahrten[0].ab == t(6, 40)
    assert zurueck.ziel_zeit == t(8, 50)
