# Zug-Bot

Plant anhand des Stundenplans die Fahrt Gelnhausen ↔ TU Darmstadt, überwacht sie live,
plant bei Problemen neu und meldet sich per ntfy. Dazu ein Dashboard fürs Handy.
Läuft auf dem Raspberry Pi Zero W (`tradingbot.tailed8a6b.ts.net`).

## Aufbau

```
iPhone: ntfy-App + Browser (ohne Tailscale)
          ▲                     ▲
       ntfy.sh          https://tradingbot.tailed8a6b.ts.net/<schlüssel>/  (Tailscale Funnel)
          │                     │
┌─────── Raspberry Pi (zugbot.service) ──────────────────────┐
│ dienst.py   Takt alle 30 s: planen, überwachen, pushen      │
│ planer.py   Suche (mehrere Anfragen) ─┐                     │
│ bewertung.py Regeln + Punkte          │                     │
│ ueberwachung.py Live-Stand, Meldungen │                     │
│ web.py      Dashboard + /api/status   │                     │
└───────────────────────────────────────┼─────────────────────┘
                                        ▼
         Transitous (Verbindungen, Echtzeit)   DB Timetables API (Gleise, optional)
```

**Warum Transitous und nicht db-rest?** db-rest lief zwar auf dem Pi, aber die DB blockt
alle Anfragen von db-vendo-client (`OPS_BLOCKED`), und dessen Doku verlangt eine Erlaubnis
der DB. Transitous ist ein offener Dienst auf Basis der DELFI-Fahrplandaten mit Echtzeit.
Bei Zügen fehlt dort oft das Gleis; das ergänzt die offizielle DB-Timetables-API, sobald
ein Schlüssel in der `.env` steht.

| Datei | Aufgabe |
|---|---|
| `config/stundenplan.yaml` | Termine, Semester, freie Tage (privat, nicht im Repo) |
| `config/einstellungen.yaml` | Haltestellen, Regeln, Puffer, Intervalle, Push-Zeiten |
| `.env` (nur auf dem Pi) | ntfy-Topic, Dashboard-Adresse, DB-Schlüssel |
| `zugbot/bewertung.py` | **Die Entscheidungslogik**, ausführlich kommentiert |
| `zugbot/planer.py` | Welche Anfragen gestellt und wie Stücke zusammengesetzt werden |
| `zugbot/ueberwachung.py` | Was als relevante Änderung gilt |
| `zugbot/gleise.py` | Gleise aus der DB-API |
| `zugbot/ics_import.py` | TUCaN-Import |
| `daten/zustand.json` | aktueller Plan, gesendete Meldungen (zum Reinschauen) |
| `deploy/` | systemd-Unit, logrotate, Installations- und Deploy-Skript |

## Was wann passiert

| Wann | Was |
|---|---|
| 21:30 | Plan für morgen neu berechnen, Push („Morgen Do: Lichtwiese …“ oder „Morgen frei“) |
| bis 60 min vor Abfahrt | alle 30 min Live-Stand abfragen (frühestens 6 h vorher) |
| 60 min vor Abfahrt in Gelnhausen | Morgen-Push mit aktuellem Stand |
| ab 60 min vor Abfahrt bis Ankunft | alle 5 min abfragen |
| Ausfall, Anschluss weg, Umstieg zu knapp | neu planen ab deinem Punkt der Reise, Push mit hoher Priorität |
| Gleiswechsel, deutlich spätere Abfahrt | Push mit normaler Priorität |
| kleine Verspätung ohne Folgen | nichts (steht nur im Dashboard) |
| Bahndaten 3x nicht erreichbar | **eine** Meldung, eine weitere, wenn es wieder geht |

## Regeln (Kurzfassung)

- Nur Nahverkehr (Deutschlandticket). ICE/IC/EC/Fernbus werden schon bei der Suche ausgeschlossen
  und zur Sicherheit nochmal geprüft.
- Immer über Frankfurt Hbf oder Frankfurt Süd. Nie Babenhausen oder Groß-Umstadt auf den
  **gefahrenen** Abschnitten (die RB zur Lichtwiese fährt danach dorthin weiter, das ist egal).
- Umstieg mind. 5 min, in Darmstadt Hbf mind. 7 min.
- Hinweg: späteste Abfahrt, die mit Puffer (5 min) vor dem ersten Termin ankommt.
  Weniger Umstiege und mehr Reserve geben Pluspunkte (`bewertung.py`, `punkte_hin`).
- Stadtmitte: Zug und Tram/Bus werden getrennt gesucht und in Darmstadt Hbf zusammengesetzt.
  Tram/Bus muss direkt an Schloss bzw. Willy-Brandt-Platz ankommen/abfahren.
- Rückweg: früheste Ankunft in Gelnhausen nach Ende des letzten Termins (+5 min).

## Einstellungen ändern

Alles in `config/einstellungen.yaml`, z. B. Ankunftspuffer, Umstiegszeiten, Intervalle,
Uhrzeit des Abendplans, Gewichte der Bewertung. Danach:

```bash
./deploy/deploy.sh
```

(vom Mac aus im Projektordner; kopiert auf den Pi und startet den Dienst neu)

Direkt auf dem Pi geht auch: Datei in `~/zugbot/config/` bearbeiten, dann
`sudo systemctl restart zugbot`. Beim nächsten Deploy vom Mac wird sie aber überschrieben,
also Änderungen besser am Mac machen und committen.

## Stundenplan

`config/stundenplan.yaml` ist von Hand lesbar und änderbar (Felder stehen oben in der Datei).
Die Datei ist **privat**: Sie steht in `.gitignore` und landet nie auf GitHub.
`deploy.sh` kopiert sie trotzdem auf den Pi. Im Repo liegt nur
`config/stundenplan.beispiel.yaml` mit erfundenen Terminen; fehlt der echte Plan,
nimmt der Bot das Beispiel. Neu anlegen: `cp config/stundenplan.beispiel.yaml config/stundenplan.yaml`.

### Neuen Stundenplan aus TUCaN importieren

1. In TUCaN: Stundenplan → Export → iCalendar (.ics) herunterladen.
2. Anschauen, was erkannt wird (ändert nichts):
   ```bash
   .venv/bin/zugbot import-ics ~/Downloads/export.ics
   ```
3. Übernehmen (alte Datei wird als `stundenplan.yaml.bak` gesichert, Semesterdaten und
   freie Tage bleiben):
   ```bash
   .venv/bin/zugbot import-ics ~/Downloads/export.ics --schreiben
   ```
4. In `config/stundenplan.yaml` alles, was du nicht besuchst, auf `aktiv: false` setzen.
   Termine mit „BITTE PRÜFEN“ haben keinen erkennbaren Raum, dort den Campus kontrollieren.
5. Testen und auf den Pi bringen:
   ```bash
   .venv/bin/zugbot plan --datum 2027-04-19 --tage 5
   ./deploy/deploy.sh
   ```

Semesterzeitraum und freie Tage (`vorlesungsfrei`) für ein neues Semester von Hand
eintragen (Quelle: tu-darmstadt.de → Semestertermine).

## Testen ohne Benachrichtigungen

```bash
zugbot plan --datum 2026-11-09 --tage 5            # auf dem Pi
.venv/bin/zugbot plan --datum 2026-11-09 --details # am Mac, mit verworfenen Verbindungen
.venv/bin/pytest                                   # Regeltests
```

## ntfy auf dem iPhone einrichten

1. App „ntfy“ aus dem App Store installieren.
2. Topic auf dem Pi nachsehen: `ssh bene635@tradingbot.tailed8a6b.ts.net grep NTFY_TOPIC zugbot/.env`
3. In der App: **+** → „Subscribe to topic“ → Topic-Namen (ohne `NTFY_TOPIC=`) eintragen,
   Server `ntfy.sh` lassen → Subscribe.
4. Mitteilungen für ntfy in den iOS-Einstellungen erlauben. Damit dringende Meldungen
   (Verbindung klappt nicht mehr) auch im Fokus-Modus durchkommen: Einstellungen → Fokus →
   jeweiligen Fokus → Apps → ntfy hinzufügen.
5. Test: `ssh bene635@tradingbot.tailed8a6b.ts.net zugbot test-push`

Der Topic-Name ist das einzige Geheimnis: Wer ihn kennt, kann mitlesen. Neu würfeln:
neuen Wert in `~/zugbot/.env` eintragen, `sudo systemctl restart zugbot`, in der App neu abonnieren.

## Gleise über die DB-API (optional)

1. Auf https://developers.deutschebahn.com registrieren, eine Anwendung anlegen und die API
   „Timetables“ abonnieren (kostenloser Plan).
2. Client-ID und API-Key auf dem Pi in `~/zugbot/.env` eintragen (`DB_CLIENT_ID`, `DB_API_KEY`).
3. EVA-Nummern der Bahnhöfe eintragen lassen: `zugbot haltestellen --eva`
   (schreibt in `config/einstellungen.yaml` auf dem Pi; danach die Datei auch am Mac übernehmen).
4. `sudo systemctl restart zugbot`

## Dashboard

Öffentlich per HTTPS über **Tailscale Funnel**, also auch auf dem Handy ohne Tailscale-App.
Adresse anzeigen (auf dem Pi):

```bash
zugbot dashboard-url
```

Die Adresse am iPhone in Safari öffnen → Teilen → „Zum Home-Bildschirm“. Die Seite
aktualisiert sich jede Minute selbst.

**Schutz:** Die Seite liegt unter `/<DASHBOARD_SCHLUESSEL>/` (zufällig, aus der `.env`),
alle anderen Pfade liefern 404. Wer die vollständige Adresse kennt, sieht deinen Fahrplan,
also nicht weitergeben. Die Seite schickt keinen Referer an andere Seiten und ist für
Suchmaschinen gesperrt. Das Dashboard selbst lauscht nur auf `127.0.0.1:8080`; von außen
kommt man nur über Funnel hinein.

Neuen Schlüssel würfeln (alte Adresse wird ungültig):

```bash
sed -i '/^DASHBOARD_SCHLUESSEL=/d' ~/zugbot/.env && bash ~/zugbot/deploy/install.sh && zugbot dashboard-url
```

Funnel einrichten (einmalig, schon erledigt):

```bash
sudo tailscale funnel --bg 8080   # beim ersten Mal kommt ein Link zur Freigabe im Tailscale-Admin
tailscale funnel status           # zeigt, was öffentlich ist
sudo tailscale funnel --https=443 off   # Funnel wieder aus
```

## Betrieb und Fehlersuche

```bash
systemctl status zugbot                 # läuft der Dienst?
journalctl -u zugbot -n 100 --no-pager  # letzte Logzeilen
tail -f /var/log/zugbot/zugbot.log      # Log-Datei (wöchentlich rotiert, 4 Wochen)
zugbot status                           # gespeicherte Pläne, letzter Fehler
zugbot neu-planen --datum 2026-11-05    # Plan eines Tages verwerfen, wird neu berechnet
sudo systemctl restart zugbot
```

Typische Probleme:

| Symptom | Ursache / Lösung |
|---|---|
| „Bahndaten nicht erreichbar“ | Transitous gestört oder Pi offline. `curl -sI https://api.transitous.org/api/v1/health` |
| Kein Push | Topic in App und `.env` vergleichen, `zugbot test-push` |
| Gleise fehlen bei Zügen | DB-Schlüssel fehlt oder EVA-Nummern leer (`zugbot haltestellen --eva`) |
| „Keine passende Hinfahrt“ | `zugbot plan --datum … --details` zeigt, warum jede Verbindung verworfen wurde |
| Falsche Zeiten nach Zeitumstellung | sollte nicht passieren (alles in Europe/Berlin); `timedatectl` prüfen |

## Datenquellen und Nutzungsregeln

- Fahrplan und Echtzeit: [Transitous](https://transitous.org) ([Quellen](https://transitous.org/sources/)),
  Kartendaten © OpenStreetMap-Mitwirkende. Regeln: https://transitous.org/api/ (User-Agent
  mit Kontakt, sparsame Abfragen, quelloffener, nicht kommerzieller Einsatz).
  Der Bot stellt etwa 10–25 Suchanfragen am Tag, Aktualisierungen laufen über `refresh-itinerary`.
- Gleise: DB Timetables API (DB InfraGO), mit eigenem Schlüssel.
