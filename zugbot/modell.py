"""Datenmodell: Abschnitt (ein Zug/Bus/Tram oder Fußweg) und Verbindung.

Wandelt die JSON-Antwort von Transitous (MOTIS) in einfache, speicherbare
Objekte um. Alle Zeiten werden nach Europe/Berlin umgerechnet, MOTIS liefert UTC.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")

# Kurzbezeichnung fürs Dashboard (Balken unter der Akzentkarte)
KURZ = {
    "tram": "Tram", "bus": "Bus", "suburban": "S", "subway": "U",
    "regional": "RB", "regionalExpress": "RE", "ferry": "Fähre",
    "nationalExpress": "ICE", "national": "IC", "coach": "Fernbus",
}

# MOTIS-Verkehrsmittel -> unsere Produktnamen (wie in erlaubte_produkte)
MODUS_PRODUKT = {
    "REGIONAL_FAST_RAIL": "regionalExpress", "SUBURBAN": "suburban", "TRAM": "tram",
    "BUS": "bus", "SUBWAY": "subway", "METRO": "subway", "FERRY": "ferry",
    "HIGHSPEED_RAIL": "nationalExpress", "LONG_DISTANCE": "national", "NIGHT_RAIL": "national",
    "COACH": "coach", "RAIL": "national",
}


def _zeit(wert: Optional[str]) -> Optional[datetime]:
    if not wert:
        return None
    # "Z" versteht fromisoformat erst ab Python 3.11, der Mac hat 3.9
    return datetime.fromisoformat(wert.replace("Z", "+00:00")).astimezone(TZ)


def _iso(wert: Optional[datetime]) -> Optional[str]:
    return wert.isoformat() if wert else None


def basis_id(stop_id: str) -> str:
    """'de-DELFI_de:06411:4734:48:48' (Steig) und 'de-DELFI_de:06411:4734' (Halt)
    gehören zum selben Halt. Auch '_G' (Haltestellengruppe) fällt weg."""
    teile = stop_id.replace("_G", "").split(":")
    return ":".join(teile[:3])


def _produkt(leg: dict) -> str:
    modus = leg.get("mode", "")
    if modus == "REGIONAL_RAIL":
        # RE und RB laufen in den DELFI-Daten beide als REGIONAL_RAIL
        name = (leg.get("displayName") or leg.get("routeShortName") or "").upper()
        return "regionalExpress" if name.startswith(("RE", "IRE")) else "regional"
    return MODUS_PRODUKT.get(modus, modus.lower())


def linie_sauber(name: str) -> str:
    """'RE50 (4503)' -> 'RE 50', 'RB82' -> 'RB 82'; 'S6', 'Tram 3', 'Bus F' bleiben.
    Die Zugnummer in Klammern hilft am Bahnsteig nicht."""
    name = re.sub(r"\s*\(\d+\)\s*$", "", name or "").strip()
    m = re.match(r"^(RE|RB|IRE)\s*(\d+)$", name, re.I)
    return f"{m.group(1).upper()} {m.group(2)}" if m else name


def _linie(leg: dict, produkt: str) -> str:
    name = linie_sauber(leg.get("displayName") or leg.get("routeShortName") or "")
    if produkt == "tram":
        return f"Tram {name}" if name and not name.lower().startswith("tram") else name or "Tram"
    if produkt == "bus":
        return f"Bus {name}" if name and not name.lower().startswith("bus") else name or "Bus"
    # "RE50" -> "RE 50", "RB82" -> "RB 82"; "S6" bleibt
    m = re.match(r"^(RE|RB|IRE)\s*(\d+)$", name, re.I)
    return f"{m.group(1).upper()} {m.group(2)}" if m else name


@dataclass
class Halt:
    id: str
    name: str

    @property
    def basis(self) -> str:
        return basis_id(self.id)


@dataclass
class Abschnitt:
    von: Halt
    nach: Halt
    ab_plan: datetime
    an_plan: datetime
    ab_ist: Optional[datetime] = None        # Prognose/Ist, None = keine Echtzeit
    an_ist: Optional[datetime] = None
    gleis_ab: Optional[str] = None
    gleis_ab_plan: Optional[str] = None
    gleis_an: Optional[str] = None
    gleis_an_plan: Optional[str] = None
    linie: str = ""                         # z. B. "RE 50", "S6", "Tram 2"
    produkt: str = ""                       # db-vendo-Produkt, z. B. regionalExpress
    richtung: str = ""
    ausfall: bool = False
    fussweg: bool = False
    distanz_m: Optional[int] = None
    trip_id: Optional[str] = None
    # Halte unterwegs (Namen), für die Babenhausen/Groß-Umstadt-Prüfung
    zwischenhalte: list[Halt] = field(default_factory=list)
    quelle: str = ""          # woher die Echtzeit stammt: "DB" oder "" (Transitous/keine)

    @property
    def ab(self) -> datetime:
        return self.ab_ist or self.ab_plan

    @property
    def an(self) -> datetime:
        return self.an_ist or self.an_plan

    @property
    def verspaetung_ab(self) -> int:
        return round((self.ab - self.ab_plan).total_seconds() / 60)

    @property
    def verspaetung_an(self) -> int:
        return round((self.an - self.an_plan).total_seconds() / 60)

    @property
    def kurz(self) -> str:
        if self.fussweg:
            return "Fuß"
        if self.produkt in ("suburban", "subway") and self.linie:
            return self.linie.replace(" ", "")       # "S6", "U4"
        return KURZ.get(self.produkt, self.linie or "?")

    @property
    def ist_steig(self) -> bool:
        """Bus und Tram haben Steige, Züge Gleise."""
        return self.produkt in ("bus", "tram")

    @property
    def fahrzeit_min(self) -> int:
        return round((self.an - self.ab).total_seconds() / 60)

    # --- Speichern/Laden ---
    def als_dict(self) -> dict:
        d = asdict(self)
        for k in ("ab_plan", "an_plan", "ab_ist", "an_ist"):
            d[k] = _iso(getattr(self, k))
        return d

    @classmethod
    def aus_dict(cls, d: dict) -> "Abschnitt":
        d = dict(d)
        d["von"] = Halt(**d["von"])
        d["nach"] = Halt(**d["nach"])
        d["zwischenhalte"] = [Halt(**h) for h in d.get("zwischenhalte", [])]
        d["linie"] = linie_sauber(d.get("linie", ""))
        for k in ("ab_plan", "an_plan", "ab_ist", "an_ist"):
            d[k] = _zeit(d.get(k))
        return cls(**d)

    # --- aus Transitous/MOTIS ---
    @classmethod
    def aus_motis(cls, leg: dict) -> "Abschnitt":
        def halt(x: dict) -> Halt:
            return Halt(id=str(x.get("stopId") or ""), name=x.get("name", "?"))

        von, nach = leg.get("from") or {}, leg.get("to") or {}
        fussweg = leg.get("mode") == "WALK"
        produkt = "" if fussweg else _produkt(leg)
        echtzeit = bool(leg.get("realTime"))
        zwischen = [halt(s) for s in leg.get("intermediateStops") or []]
        # Ein Ausfall kann am Abschnitt oder an Ab-/Ankunftshalt hängen
        ausfall = bool(leg.get("cancelled") or von.get("cancelled") or nach.get("cancelled"))
        return cls(
            von=halt(von),
            nach=halt(nach),
            ab_plan=_zeit(leg.get("scheduledStartTime") or leg.get("startTime")),
            an_plan=_zeit(leg.get("scheduledEndTime") or leg.get("endTime")),
            ab_ist=_zeit(leg.get("startTime")) if echtzeit else None,
            an_ist=_zeit(leg.get("endTime")) if echtzeit else None,
            # Bei Tram/Bus liefert DELFI den Steig, bei Zügen meist nicht (-> gleise.py)
            gleis_ab=von.get("track"),
            gleis_ab_plan=von.get("scheduledTrack"),
            gleis_an=nach.get("track"),
            gleis_an_plan=nach.get("scheduledTrack"),
            linie="" if fussweg else _linie(leg, produkt),
            produkt=produkt,
            richtung=leg.get("headsign") or "",
            ausfall=ausfall,
            fussweg=fussweg,
            distanz_m=round(leg["distance"]) if leg.get("distance") else None,
            trip_id=leg.get("tripId"),
            zwischenhalte=zwischen,
        )


@dataclass
class Teil:
    """Ein zusammenhängendes Stück mit eigenem refreshToken.

    Stadtmitte-Fahrten bestehen aus zwei Teilen (Zug + Tram/Bus), weil sie
    getrennt gesucht werden. Lichtwiese-Fahrten haben einen Teil.
    refresh_token ist die Verbindungs-ID von MOTIS (für refresh-itinerary).
    """
    abschnitte: list[Abschnitt]
    refresh_token: Optional[str] = None

    def als_dict(self) -> dict:
        return {"refresh_token": self.refresh_token,
                "abschnitte": [a.als_dict() for a in self.abschnitte]}

    @classmethod
    def aus_dict(cls, d: dict) -> "Teil":
        return cls([Abschnitt.aus_dict(a) for a in d["abschnitte"]], d.get("refresh_token"))

    @classmethod
    def aus_motis(cls, itinerary: dict) -> "Teil":
        abschnitte = [Abschnitt.aus_motis(l) for l in itinerary.get("legs", [])]
        # Fußwege von 0 Minuten am selben Halt (MOTIS-Eigenheit) weglassen
        abschnitte = [a for a in abschnitte if not (a.fussweg and a.an_plan <= a.ab_plan)]
        return cls(abschnitte, itinerary.get("id"))


@dataclass
class Verbindung:
    teile: list[Teil]
    richtung: str                  # "hin" oder "rueck"
    campus: str
    ziel_zeit: Optional[datetime] = None   # Hinweg: spätestens hier ankommen
    neu_geplant: bool = False
    hinweis: str = ""

    @property
    def abschnitte(self) -> list[Abschnitt]:
        return [a for t in self.teile for a in t.abschnitte]

    @property
    def fahrten(self) -> list[Abschnitt]:
        """Nur Fahrzeuge, ohne Fußwege."""
        return [a for a in self.abschnitte if not a.fussweg]

    @property
    def ab(self) -> datetime:
        return self.fahrten[0].ab

    @property
    def ab_plan(self) -> datetime:
        return self.fahrten[0].ab_plan

    @property
    def an(self) -> datetime:
        return self.fahrten[-1].an

    @property
    def an_plan(self) -> datetime:
        return self.fahrten[-1].an_plan

    @property
    def umstiege(self) -> int:
        return max(0, len(self.fahrten) - 1)

    @property
    def dauer_min(self) -> int:
        return round((self.an - self.ab).total_seconds() / 60)

    def schluessel(self) -> str:
        """Erkennt 'dieselbe' Verbindung wieder (Linien + Planzeiten)."""
        return "|".join(f"{a.linie}@{a.ab_plan.strftime('%H%M')}" for a in self.fahrten)

    def als_dict(self) -> dict:
        return {
            "teile": [t.als_dict() for t in self.teile],
            "richtung": self.richtung,
            "campus": self.campus,
            "ziel_zeit": _iso(self.ziel_zeit),
            "neu_geplant": self.neu_geplant,
            "hinweis": self.hinweis,
        }

    @classmethod
    def aus_dict(cls, d: dict) -> "Verbindung":
        return cls(
            teile=[Teil.aus_dict(t) for t in d["teile"]],
            richtung=d["richtung"],
            campus=d["campus"],
            ziel_zeit=_zeit(d.get("ziel_zeit")),
            neu_geplant=d.get("neu_geplant", False),
            hinweis=d.get("hinweis", ""),
        )
