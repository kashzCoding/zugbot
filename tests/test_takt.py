"""Takt der letzten Meile: welche Trams/Busse passen zu welcher Verbindung."""

from datetime import datetime, timedelta

from zugbot import config, takt
from zugbot.bewertung import Regeln
from zugbot.modell import TZ, Abschnitt, Halt, Teil, Verbindung

REGELN = Regeln.aus_einstellungen(config.einstellungen())
DA = Halt(config.einstellungen()["haltestellen"]["darmstadt_hbf"]["id"], "Darmstadt Hauptbahnhof")
FFM = Halt("ffm", "Frankfurt (Main) Hauptbahnhof")
CAMPUS = Halt("c", "Darmstadt TU-Lichtwiese/Campus")


def t(h, m):
    return datetime(2026, 11, 5, h, m, tzinfo=TZ)


def fahrten(linie, erste, abstand, anzahl, dauer=18, nach="Campus"):
    return [{"ab": (erste + timedelta(minutes=i * abstand)).isoformat(),
             "an": (erste + timedelta(minutes=i * abstand + dauer)).isoformat(),
             "linie": linie, "nach": nach} for i in range(anzahl)]


def hinweg(zug_an, tram_ab=None, an_ist=None):
    zug = Abschnitt(FFM, DA, t(7, 0), zug_an, an_ist=an_ist, linie="RB 68", produkt="regional")
    abschnitte = [zug]
    if tram_ab:
        abschnitte.append(Abschnitt(DA, CAMPUS, tram_ab, tram_ab + timedelta(minutes=18),
                                    linie="Tram 2", produkt="tram"))
    return Verbindung([Teil(abschnitte)], "hin", "lichtwiese", ziel_zeit=t(7, 55))


def test_takt_text():
    assert takt.takt_text([t(7, 0), t(7, 10), t(7, 20), t(7, 30)]) == "etwa alle 10 min"
    assert takt.takt_text([t(7, 0), t(7, 5), t(7, 15), t(7, 20), t(7, 30)]) == "etwa alle 5–10 min"
    assert takt.takt_text([t(7, 0)]) == ""


def test_hinweg_nur_nach_umstieg_und_rechtzeitig():
    stadt = {"fahrten": fahrten("Tram 2", t(7, 20), 5, 6)}   # 7:20 ... 7:45, je 18 min
    i = takt.info(stadt, hinweg(zug_an=t(7, 22), tram_ab=t(7, 30)), REGELN)
    # frühestens 7:22 + 7 min Umstieg, spätestens an 7:55 -> ab 7:30, 7:35
    assert [f["ab"] for f in i["fahrten"]] == ["07:30", "07:35"]
    assert i["fahrten"][0]["deine"] and not i["fahrten"][1]["deine"]
    assert i["text"] == "Tram 2 etwa alle 5 min"


def test_hinweg_verspaetung_zaehlt():
    stadt = {"fahrten": fahrten("Tram 2", t(7, 20), 5, 6)}
    i = takt.info(stadt, hinweg(zug_an=t(7, 22), an_ist=t(7, 27)), REGELN)
    assert [f["ab"] for f in i["fahrten"]] == ["07:35"]


def test_mehrere_linien_zusammen():
    stadt = {"fahrten": sorted(fahrten("Tram 2", t(7, 30), 10, 3, dauer=8) + fahrten("Bus K", t(7, 35), 10, 3, dauer=8),
                               key=lambda f: f["ab"])}
    i = takt.info(stadt, hinweg(zug_an=t(7, 20)), REGELN)
    assert i["linien"] == "Tram 2, Bus K"
    assert i["takt"] == "zusammen etwa alle 5 min"


def test_rueckweg_bis_zugabfahrt_minus_umstieg():
    zug = Abschnitt(DA, FFM, t(16, 30), t(16, 47), linie="RB 68", produkt="regional")
    v = Verbindung([Teil([zug])], "rueck", "stadtmitte")
    stadt = {"ab": t(16, 10).isoformat(), "fahrten": fahrten("Bus H", t(16, 5), 5, 6, dauer=8)}
    i = takt.info(stadt, v, REGELN)
    # ab frühestens 16:10, an spätestens 16:23 -> 16:10 (an 16:18), 16:15 (an 16:23)
    assert [f["ab"] for f in i["fahrten"]] == ["16:10", "16:15"]
    assert i["titel"] == "Zum Darmstadt Hbf"


def test_ohne_darmstadt_hbf_nichts():
    direkt = Abschnitt(FFM, CAMPUS, t(7, 26), t(7, 50), linie="RB 82", produkt="regional")
    v = Verbindung([Teil([direkt])], "hin", "lichtwiese", ziel_zeit=t(7, 55))
    assert takt.info({"fahrten": fahrten("Tram 2", t(7, 20), 5, 6)}, v, REGELN) is None


def test_eigene_fahrt_immer_sichtbar():
    stadt = {"fahrten": fahrten("Bus H", t(7, 30), 2, 10, dauer=8)}   # 7:30 ... 7:48
    i = takt.info(stadt, hinweg(zug_an=t(7, 20), tram_ab=None), REGELN)
    assert len(i["fahrten"]) == takt.ANZEIGEN_HOECHSTENS and i["fahrten"][0]["ab"] == "07:30"
    v = hinweg(zug_an=t(7, 20))
    v.teile[0].abschnitte.append(Abschnitt(DA, CAMPUS, t(7, 46), t(7, 54), linie="Bus H", produkt="bus"))
    i = takt.info(stadt, v, REGELN)
    assert len(i["fahrten"]) == takt.ANZEIGEN_HOECHSTENS
    assert i["fahrten"][-1] == {**i["fahrten"][-1], "ab": "07:46", "deine": True}
