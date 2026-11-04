"""Tests mit dem Beispiel-Stundenplan (der echte ist privat und nicht im Repo)."""

from datetime import date

import pytest

from zugbot import config, stundenplan


@pytest.fixture(scope="module")
def plan():
    return config.lade_yaml("stundenplan.beispiel.yaml")


def test_inaktiver_termin_zaehlt_nicht(plan):
    t = stundenplan.tag(date(2026, 11, 11), plan)          # Mittwoch
    assert t.erster.von.strftime("%H:%M") == "13:30"


def test_campus_pro_tag(plan):
    erwartet = {9: "stadtmitte", 10: "lichtwiese", 11: "stadtmitte", 12: "lichtwiese"}
    for tag, campus in erwartet.items():
        t = stundenplan.tag(date(2026, 11, tag), plan)
        assert t.campus == campus and not t.warnungen
    assert stundenplan.tag(date(2026, 11, 13), plan).frei  # Freitag ohne Termine


def test_weihnachtspause_und_wochenende_frei(plan):
    assert stundenplan.tag(date(2026, 12, 22), plan).frei
    assert stundenplan.tag(date(2027, 1, 8), plan).frei
    assert not stundenplan.tag(date(2027, 1, 11), plan).frei
    assert stundenplan.tag(date(2026, 11, 14), plan).frei   # Samstag
    assert stundenplan.tag(date(2027, 2, 15), plan).frei    # nach Vorlesungsende


def test_raumwechsel(plan):
    assert stundenplan.tag(date(2026, 12, 15), plan).erster.raum == "L1|01 100"
    assert stundenplan.tag(date(2027, 1, 12), plan).erster.raum == "L1|01 200"


def test_campus_von_hand_bei_raum_offen(plan):
    t = stundenplan.tag(date(2026, 11, 12), plan)
    assert t.erster.campus == "lichtwiese"


def test_warnung_bei_zwei_campus():
    zwei = {"termine": [
        {"tag": "Mo", "von": "08:00", "bis": "09:40", "raum": "S1|01 A1", "titel": "A"},
        {"tag": "Mo", "von": "11:40", "bis": "13:20", "raum": "L4|02 1", "titel": "B"},
    ]}
    t = stundenplan.tag(date(2026, 11, 9), zwei)
    assert t.campus == "stadtmitte" and t.warnungen
