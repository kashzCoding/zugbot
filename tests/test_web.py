"""Das Dashboard ist öffentlich (Tailscale Funnel) und nur durch den Schlüssel geschützt,
auch die beiden POST-Endpunkte zum Wählen und Anpassen."""

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

import pytest

from zugbot import config, web, wuensche, zustand

SCHLUESSEL = "testschluessel-abc123"


@pytest.fixture(scope="module")
def basis():
    web.Handler.schluessel = SCHLUESSEL
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    web.Handler.schluessel = ""


class OhneWeiterleitung(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def status(url):
    oeffner = urllib.request.build_opener(OhneWeiterleitung)
    try:
        with oeffner.open(url) as r:
            return r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)


@pytest.mark.parametrize("pfad", ["/", "/index.html", "/api/status", "/api/wecker", "/falsch/",
                                  "/falsch/api/status", "/falsch/api/wecker",
                                  f"/{SCHLUESSEL}x/", f"/{SCHLUESSEL[:-1]}/"])
def test_ohne_richtigen_schluessel_404(basis, pfad):
    assert status(basis + pfad)[0] == 404


def test_mit_schluessel_seite(basis):
    code, kopf = status(f"{basis}/{SCHLUESSEL}/")
    assert code == 200
    assert kopf["Referrer-Policy"] == "no-referrer"
    assert "noindex" in kopf["X-Robots-Tag"]


@pytest.mark.parametrize("datei", ["logo.svg", "apple-touch-icon.png", "favicon-32.png"])
def test_bilder_nur_mit_schluessel(basis, datei):
    assert status(f"{basis}/{SCHLUESSEL}/{datei}")[0] == 200
    assert status(f"{basis}/{datei}")[0] == 404


def test_keine_anderen_dateien(basis):
    for pfad in ("index.html/../web.py", "../config.py", "static/logo.svg", "web.py"):
        assert status(f"{basis}/{SCHLUESSEL}/{pfad}")[0] == 404


def test_ohne_schraegstrich_weiterleitung(basis):
    code, kopf = status(f"{basis}/{SCHLUESSEL}")
    assert code == 301 and kopf["Location"] == f"/{SCHLUESSEL}/"


# ------------------------------------------------------------ POST (Wünsche)
def post(url, inhalt, typ="application/json"):
    daten = inhalt if isinstance(inhalt, bytes) else json.dumps(inhalt).encode()
    anfrage = urllib.request.Request(url, data=daten, method="POST", headers={"Content-Type": typ})
    try:
        with urllib.request.urlopen(anfrage) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


@pytest.fixture
def wunschdatei(tmp_path, monkeypatch):
    monkeypatch.setattr(wuensche, "PFAD", tmp_path / "wuensche.json")
    monkeypatch.setattr(zustand, "PFAD", tmp_path / "zustand.json")
    heute = datetime.now(config.zeitzone()).date()
    return heute + timedelta(days=1)


@pytest.mark.parametrize("pfad", ["/api/anpassen", "/falsch/api/anpassen", f"/{SCHLUESSEL}/api/status",
                                  f"/{SCHLUESSEL}/"])
def test_post_nur_mit_schluessel_und_bekannter_route(basis, wunschdatei, pfad):
    assert post(basis + pfad, {"datum": "morgen", "an": "10:00"})[0] == 404
    assert not wuensche.PFAD.exists()


def test_post_nur_json(basis, wunschdatei):
    assert post(f"{basis}/{SCHLUESSEL}/api/anpassen", b"datum=morgen&an=10:00",
                "application/x-www-form-urlencoded")[0] == 415
    assert post(f"{basis}/{SCHLUESSEL}/api/anpassen", b"x" * 5000)[0] == 413
    assert post(f"{basis}/{SCHLUESSEL}/api/anpassen", b"{kaputt")[0] == 400


def test_anpassen_speichert(basis, wunschdatei):
    code, j = post(f"{basis}/{SCHLUESSEL}/api/anpassen", {"datum": wunschdatei.isoformat(), "an": "10:00"})
    assert code == 200 and j["ok"]
    assert wuensche.anpassung(wuensche.laden(), wunschdatei)["an"] == "10:00"
    code, j = post(f"{basis}/{SCHLUESSEL}/api/anpassen", {"datum": wunschdatei.isoformat(), "zuruecksetzen": True})
    assert code == 200 and wuensche.anpassung(wuensche.laden(), wunschdatei) is None


def test_anpassen_falsche_eingabe(basis, wunschdatei):
    code, j = post(f"{basis}/{SCHLUESSEL}/api/anpassen", {"datum": "morgen", "an": "26:00"})
    assert code == 400 and "Uhrzeit" in j["fehler"]


def test_waehlen_unbekannt(basis, wunschdatei):
    code, j = post(f"{basis}/{SCHLUESSEL}/api/waehlen", {"datum": "morgen", "richtung": "hin", "schluessel": "x"})
    assert code == 400 and not wuensche.PFAD.exists()
