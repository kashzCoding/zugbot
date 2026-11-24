"""„Verspätete Ankunft“ nicht bei jeder Minute erneut pushen."""

from datetime import datetime, timedelta

from zugbot import config, ueberwachung
from zugbot.bewertung import Regeln
from zugbot.modell import TZ, Abschnitt, Halt, Teil, Verbindung

REGELN = Regeln.aus_einstellungen(config.einstellungen())
GELN = Halt("de-DELFI_de:06435:11058", "Gelnhausen Bahnhof")
LW = Halt("de-DELFI_de:06411:16027", "Darmstadt TU-Lichtwiese Bahnhof")


def t(h, m):
    return datetime(2026, 11, 24, h, m, tzinfo=TZ)


def fahrt(an_ist):
    a = Abschnitt(GELN, LW, t(7, 7), t(8, 45), ab_ist=t(7, 7), an_ist=an_ist, linie="RE 50", produkt="regional")
    return Verbindung([Teil([a])], "hin", "lichtwiese", ziel_zeit=t(8, 50))


def test_heute_frueh_nachgespielt():
    """Am 24.11. kamen 5 Pushes, je 1 min Unterschied. Jetzt nur bei >= 5 min."""
    gemeldet, pushes = {}, []
    jetzt = t(8, 15)
    for an in (t(8, 51), t(8, 53), t(8, 55), t(8, 56), t(9, 7), t(9, 6), t(9, 7), t(9, 8), t(9, 11)):
        v = fahrt(an)
        m = [x for x in ueberwachung.lage(v, REGELN, jetzt, gemeldet, 99) if x.art == "zu_spaet"]
        pushes += [f"{an:%H:%M}"] if m else []
        gemeldet = ueberwachung.zu_merken(v, REGELN, jetzt, gemeldet)
        jetzt += timedelta(minutes=5)
    # 08:51 gemeldet; 08:53/08:55 nicht; 08:56 (+5 zu 08:51) ja; 09:07 ja; 09:11 (+4) nicht
    assert pushes == ["08:51", "08:56", "09:07"]


def test_frueher_zaehlt_auch():
    gemeldet = ueberwachung.zu_merken(fahrt(t(9, 10)), REGELN, t(8, 0), {})
    assert ueberwachung.lage(fahrt(t(9, 5)), REGELN, t(8, 0), gemeldet, 99)
    assert not [m for m in ueberwachung.lage(fahrt(t(9, 7)), REGELN, t(8, 0), gemeldet, 99) if m.art == "zu_spaet"]


def test_wieder_puenktlich_entwarnung():
    gemeldet = ueberwachung.zu_merken(fahrt(t(8, 55)), REGELN, t(8, 0), {})
    m = ueberwachung.lage(fahrt(t(8, 48)), REGELN, t(8, 0), gemeldet, 99)
    assert [x.art for x in m] == ["entwarnung"]
