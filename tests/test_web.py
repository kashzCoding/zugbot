"""Das Dashboard ist öffentlich (Tailscale Funnel) und nur durch den Schlüssel geschützt."""

import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from zugbot import web

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
