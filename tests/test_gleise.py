"""Echtzeit für Züge aus der DB-Tafel (nachgebaute Antworten, kein Netz)."""

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import pytest

from zugbot import config, gleise
from zugbot.modell import TZ, Abschnitt, Halt, Teil, Verbindung

E = config.einstellungen()
GEL = Halt(E["haltestellen"]["gelnhausen"]["id"], "Gelnhausen Bahnhof")
FFM = Halt(E["haltestellen"]["frankfurt_hbf"]["id"], "Frankfurt (Main) Hauptbahnhof")


def tafel(*eintraege):
    return ET.fromstring("<timetable>" + "".join(eintraege) + "</timetable>")


@pytest.fixture
def db(monkeypatch):
    g = gleise.DbGleise(E)
    g.aktiv = True
    antworten = {}
    monkeypatch.setattr(g, "_xml", lambda pfad, alter: antworten.get(pfad.split("/")[1] + ":" + pfad.split("/")[2]))
    return g, antworten


def zug(ab):
    a = Abschnitt(GEL, FFM, ab, ab + timedelta(minutes=40), linie="RE 50", produkt="regionalExpress")
    return Verbindung([Teil([a])], "hin", "lichtwiese")


def test_verspaetung_und_gleiswechsel(db, monkeypatch):
    g, antworten = db
    ab = datetime.now(TZ).replace(second=0, microsecond=0) + timedelta(minutes=30)
    pt = ab.strftime("%y%m%d%H%M")
    eva = E["haltestellen"]["gelnhausen"]["eva"]
    antworten[f"plan:{eva}"] = tafel(f'<s id="1"><dp pt="{pt}" pp="2" l="RE50"/></s>')
    antworten[f"fchg:{eva}"] = tafel(f'<s id="1"><dp ct="{(ab + timedelta(minutes=12)).strftime("%y%m%d%H%M")}" cp="3"/></s>')
    v = zug(ab)
    g.ergaenzen(v)
    a = v.fahrten[0]
    assert a.verspaetung_ab == 12 and a.gleis_ab == "3" and a.quelle == "DB" and not a.ausfall


def test_ausfall(db):
    g, antworten = db
    ab = datetime.now(TZ).replace(second=0, microsecond=0) + timedelta(minutes=30)
    eva = E["haltestellen"]["gelnhausen"]["eva"]
    antworten[f"plan:{eva}"] = tafel(f'<s id="1"><dp pt="{ab:%y%m%d%H%M}" pp="2" l="RE50"/></s>')
    antworten[f"fchg:{eva}"] = tafel('<s id="1"><dp cs="c"/></s>')
    v = zug(ab)
    g.ergaenzen(v)
    assert v.fahrten[0].ausfall


def test_ohne_aenderung_kurz_vorher_puenktlich_am_vorabend_unbekannt(db):
    g, antworten = db
    eva = E["haltestellen"]["gelnhausen"]["eva"]
    for minuten, erwartet in ((30, 0), (600, None)):
        ab = datetime.now(TZ).replace(second=0, microsecond=0) + timedelta(minutes=minuten)
        antworten[f"plan:{eva}"] = tafel(f'<s id="1"><dp pt="{ab:%y%m%d%H%M}" pp="2" l="RE50"/></s>')
        antworten[f"fchg:{eva}"] = tafel()
        v = zug(ab)
        g.ergaenzen(v)
        a = v.fahrten[0]
        assert (a.verspaetung_ab if a.ab_ist else None) == erwartet


def test_andere_linie_gleiche_minute_nicht_verwechseln(db):
    g, antworten = db
    ab = datetime.now(TZ).replace(second=0, microsecond=0) + timedelta(minutes=30)
    eva = E["haltestellen"]["gelnhausen"]["eva"]
    antworten[f"plan:{eva}"] = tafel(f'<s id="1"><dp pt="{ab:%y%m%d%H%M}" pp="5" l="RB46"/></s>')
    antworten[f"fchg:{eva}"] = tafel('<s id="1"><dp cs="c"/></s>')
    v = zug(ab)
    g.ergaenzen(v)
    assert not v.fahrten[0].ausfall and v.fahrten[0].quelle == ""


@pytest.mark.parametrize("kat,l,erwartet", [("RE", "RE50", "RE 50"), ("VIA", "82", "RB 82"),
                                            ("S", "6", "S6"), ("HLB", "51", "RB 51"), ("RB", "RB82", "RB 82")])
def test_linienname_wie_transitous(kat, l, erwartet):
    assert gleise._linie(kat, l) == erwartet
