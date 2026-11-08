"""Lädt einstellungen.yaml, stundenplan.yaml und die .env."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

PROJEKT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("ZUGBOT_CONFIG", PROJEKT / "config"))
DATEN_DIR = Path(os.environ.get("ZUGBOT_DATEN", PROJEKT / "daten"))


def _lade_env(pfad: Path) -> None:
    """Minimaler .env-Leser (KEY=wert pro Zeile), ohne Zusatzpaket."""
    if not pfad.exists():
        return
    for zeile in pfad.read_text(encoding="utf-8").splitlines():
        zeile = zeile.strip()
        if not zeile or zeile.startswith("#") or "=" not in zeile:
            continue
        key, wert = zeile.split("=", 1)
        os.environ.setdefault(key.strip(), wert.strip().strip('"').strip("'"))


_lade_env(PROJEKT / ".env")


@dataclass
class Env:
    transitous_url: str
    user_agent: str
    ntfy_server: str
    ntfy_topic: str
    web_host: str
    db_client_id: str
    db_api_key: str
    dashboard_schluessel: str


def env() -> Env:
    return Env(
        transitous_url=os.environ.get("TRANSITOUS_URL", "https://api.transitous.org").rstrip("/"),
        user_agent=os.environ.get("USER_AGENT", "zugbot/1.0 (+https://github.com/kashzCoding/zugbot)"),
        ntfy_server=os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
        ntfy_topic=os.environ.get("NTFY_TOPIC", ""),
        web_host=os.environ.get("WEB_HOST", "127.0.0.1"),
        db_client_id=os.environ.get("DB_CLIENT_ID", ""),
        db_api_key=os.environ.get("DB_API_KEY", ""),
        dashboard_schluessel=os.environ.get("DASHBOARD_SCHLUESSEL", ""),
    )


# Zwischenspeicher: Datei -> (Änderungszeit, Inhalt). YAML lesen kostet auf dem
# Pi Zero ~0,3 s pro Datei, und Dashboard und Dienst fragen oft danach. Neu
# gelesen wird nur, wenn sich die Datei geändert hat.
_yaml_cache: dict[str, tuple[float, dict]] = {}
_Lader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)   # C-Parser, falls installiert


def lade_yaml(name: str) -> dict:
    pfad = CONFIG_DIR / name
    stand = pfad.stat().st_mtime
    alt = _yaml_cache.get(name)
    if alt is None or alt[0] != stand:
        with open(pfad, encoding="utf-8") as f:
            alt = (stand, yaml.load(f, Loader=_Lader) or {})
        _yaml_cache[name] = alt
    # Kopie, damit ein Aufrufer, der etwas ändert, den Zwischenspeicher nicht verändert
    return copy.deepcopy(alt[1])


def einstellungen() -> dict:
    return lade_yaml("einstellungen.yaml")


def stundenplan_roh() -> dict:
    # Der echte Stundenplan ist privat (.gitignore). Im Repo liegt nur das Beispiel.
    if (CONFIG_DIR / "stundenplan.yaml").exists():
        return lade_yaml("stundenplan.yaml")
    return lade_yaml("stundenplan.beispiel.yaml")


def zeitzone() -> ZoneInfo:
    return ZoneInfo(lade_yaml("einstellungen.yaml").get("zeitzone", "Europe/Berlin"))


def schreibe_haltestellen_feld(feld: str, werte: dict[str, str]) -> None:
    """Trägt z. B. EVA-Nummern in einstellungen.yaml ein, ohne Kommentare zu zerstören.

    Ersetzt in Zeilen der Form `  schluessel: {name: "...", id: "...", eva: ...}`
    den Wert hinter `feld:`.
    """
    import re

    pfad = CONFIG_DIR / "einstellungen.yaml"
    zeilen = pfad.read_text(encoding="utf-8").splitlines(keepends=True)
    neu = []
    for zeile in zeilen:
        stripped = zeile.strip()
        for key, wert in werte.items():
            if stripped.startswith(f"{key}:") and "{" in stripped and f"{feld}:" in stripped:
                zeile = re.sub(rf"{feld}: [^,}}]*", f'{feld}: "{wert}"', zeile)
        neu.append(zeile)
    pfad.write_text("".join(neu), encoding="utf-8")
