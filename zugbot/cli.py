"""Befehle: zugbot plan | run | haltestellen | import-ics | test-push | neu-planen | status"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config


def logging_einrichten(datei: bool) -> None:
    handler: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if datei:
        log_dir = Path("/var/log/zugbot")   # legt deploy/install.sh an
        if not (log_dir.is_dir() and os.access(log_dir, os.W_OK)):
            log_dir = config.DATEN_DIR
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            # Rotation übernimmt logrotate (deploy/logrotate-zugbot), daher WatchedFileHandler
            handler.append(logging.handlers.WatchedFileHandler(log_dir / "zugbot.log", encoding="utf-8"))
        except OSError:
            pass
    logging.basicConfig(level=logging.INFO, handlers=handler,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def cmd_plan(args) -> int:
    """Plant Hin- und Rückfahrt für ein Datum und zeigt sie an. Keine Benachrichtigung."""
    from . import stundenplan, texte
    from .bahn import Bahn
    from .planer import Planer

    e = config.einstellungen()
    planer = Planer(Bahn(e), e)
    tage = [date.fromisoformat(args.datum) + timedelta(days=i) for i in range(args.tage)]
    for d in tage:
        t = stundenplan.tag(d)
        wt = stundenplan.WOCHENTAGE_LANG[d.weekday()]
        print(f"\n=== {wt}, {d:%d.%m.%Y} ===")
        for w in t.warnungen:
            print(f"  WARNUNG: {w}")
        if t.frei:
            print(f"  frei ({t.grund}) -> abends kommt „Morgen frei“")
            continue
        print(f"  Campus {texte.campus_name(t.campus)}, erster Termin {t.erster.von:%H:%M} "
              f"{t.erster.titel} ({t.erster.raum}), Ende {t.letzter.bis:%H:%M}")
        hin = planer.hinweg(t)
        print(f"\n  Hinfahrt (an spätestens {planer.ziel_zeit(t):%H:%M}, "
              f"{hin.kandidaten} Kandidaten geprüft):")
        print(texte.terminal(hin.verbindung, planer.regeln))
        rueck = planer.rueckweg(t)
        print(f"\n  Rückfahrt (ab frühestens {planer.frueheste_rueckfahrt(t):%H:%M}, "
              f"{rueck.kandidaten} Kandidaten geprüft):")
        print(texte.terminal(rueck.verbindung, planer.regeln))
        if args.details:
            for name, erg in (("Hin", hin), ("Rück", rueck)):
                for v in erg.verworfen:
                    print(f"    verworfen ({name}): {v}")
    print(f"\n({planer.bahn.anfragen_heute.get(date.today().isoformat(), 0)} Anfragen an Transitous)")
    return 0


def cmd_haltestellen(args) -> int:
    """Zeigt Treffer für die Haltestellennamen (Transitous) bzw. trägt EVA-Nummern ein."""
    from .bahn import Bahn

    e = config.einstellungen()
    if args.eva:
        from . import gleise
        db = gleise.DbGleise()
        if not db.aktiv:
            print("DB_CLIENT_ID und DB_API_KEY fehlen in der .env.")
            return 1
        gefunden = {}
        for key, h in e["haltestellen"].items():
            if "eva" not in h:
                continue
            treffer = db.eva_suchen(h["name"])
            print(f"{key:<20} {h['name']:<34} -> " +
                  (f"{treffer[0]} ({treffer[1]})" if treffer else "nicht gefunden"))
            if treffer:
                gefunden[key] = treffer[0]
        config.schreibe_haltestellen_feld("eva", gefunden)
        print("EVA-Nummern in config/einstellungen.yaml eingetragen.")
        return 0
    bahn = Bahn(e)
    for key, h in e["haltestellen"].items():
        treffer = bahn.haltestelle_suchen(h["name"])[:3]
        print(f"{key:<20} konfiguriert: {h.get('id')}")
        for t in treffer:
            print(f"{'':<20}   {t['id']:<40} {t['name']}")
    return 0


def cmd_import(args) -> int:
    from . import ics_import
    return ics_import.importieren(Path(args.datei), schreiben=args.schreiben)


def cmd_test_push(args) -> int:
    from . import ntfy
    ok = ntfy.senden("Zug-Bot Test", "Wenn du das liest, kommen die Benachrichtigungen an.", "normal")
    print("gesendet" if ok else "fehlgeschlagen, siehe Meldung oben")
    return 0 if ok else 1


def cmd_neu_planen(args) -> int:
    """Verwirft den gespeicherten Plan eines Tages; der Dienst plant ihn im nächsten Takt neu."""
    from . import zustand
    z = zustand.laden()
    weg = z.get("plaene", {}).pop(args.datum, None)
    z.get("gemeldet", {}).pop(f"{args.datum}:hin", None)
    z.get("gemeldet", {}).pop(f"{args.datum}:rueck", None)
    zustand.speichern(z)
    print("Plan verworfen, wird neu geplant." if weg else "Für diesen Tag war kein Plan gespeichert.")
    return 0


def cmd_status(args) -> int:
    from . import zustand
    z = zustand.laden()
    print(json.dumps(z.get("status", {}), ensure_ascii=False, indent=1))
    for d, p in sorted(z.get("plaene", {}).items()):
        hin = (p.get("hin") or {}).get("teile")
        print(f"{d}: {'frei' if p['tag']['frei'] else p['tag']['campus']}, "
              f"hin {'ja' if hin else 'nein'}, rück {'ja' if p.get('rueck') else 'nein'}"
              f"{', FEHLER: ' + p['fehler'] if p.get('fehler') else ''}")
    return 0


def cmd_dashboard_url(args) -> int:
    """Gibt die öffentliche Funnel-Adresse des Dashboards aus (mit Schlüssel)."""
    import subprocess

    schluessel = config.env().dashboard_schluessel
    if not schluessel:
        print("DASHBOARD_SCHLUESSEL fehlt in der .env (deploy/install.sh legt ihn an).")
        return 1
    try:
        st = json.loads(subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                                       text=True, timeout=10).stdout)
        host = st["Self"]["DNSName"].rstrip(".")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        host = "<pi-name>.<tailnet>.ts.net"
    print(f"https://{host}/{schluessel}/")
    return 0


def cmd_run(args) -> int:
    from . import dienst
    dienst.starten()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="zugbot", description="Zug-Bot für den Pendelweg Gelnhausen - TU Darmstadt")
    sub = ap.add_subparsers(dest="befehl", required=True)

    p = sub.add_parser("plan", help="Hin- und Rückfahrt für ein Datum anzeigen (ohne Benachrichtigung)")
    p.add_argument("--datum", default=date.today().isoformat(), help="JJJJ-MM-TT, Standard heute")
    p.add_argument("--tage", type=int, default=1, help="so viele Tage ab --datum")
    p.add_argument("--details", action="store_true", help="auch verworfene Verbindungen mit Grund zeigen")
    p.set_defaults(f=cmd_plan)

    p = sub.add_parser("haltestellen", help="Haltestellen-IDs prüfen")
    p.add_argument("--eva", action="store_true", help="DB-EVA-Nummern für Gleise eintragen (braucht DB_API_KEY)")
    p.set_defaults(f=cmd_haltestellen)

    p = sub.add_parser("import-ics", help="Stundenplan aus TUCaN-iCal-Export übernehmen")
    p.add_argument("datei")
    p.add_argument("--schreiben", action="store_true",
                   help="config/stundenplan.yaml ersetzen (alte Datei wird gesichert)")
    p.set_defaults(f=cmd_import)

    sub.add_parser("test-push", help="Test-Nachricht über ntfy senden").set_defaults(f=cmd_test_push)
    p = sub.add_parser("neu-planen", help="gespeicherten Plan eines Tages verwerfen")
    p.add_argument("--datum", default=date.today().isoformat())
    p.set_defaults(f=cmd_neu_planen)
    sub.add_parser("status", help="Zustand des Dienstes anzeigen").set_defaults(f=cmd_status)
    sub.add_parser("dashboard-url", help="öffentliche Dashboard-Adresse anzeigen").set_defaults(
        f=cmd_dashboard_url)
    sub.add_parser("run", help="Dienst starten (macht systemd)").set_defaults(f=cmd_run)

    args = ap.parse_args(argv)
    logging_einrichten(datei=args.befehl == "run")
    if args.befehl != "run":
        logging.getLogger().setLevel(logging.WARNING)
    return args.f(args)


if __name__ == "__main__":
    sys.exit(main())
