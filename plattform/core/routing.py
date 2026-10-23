"""Fahrzeiten, Touren und Navigation auf Basis von OSRM (Open Source Routing Machine, BSD-2).

OSRM läuft selbst gehostet (Compose-Profil `routing`, Kartenausschnitt aus Geofabrik) oder, nur für Tests und
geringe Last, über den öffentlichen Demo-Server; `ROUTING_URL` bzw. Stammdatum `routing_url`. Ohne Dienst
(`ROUTING_AUS=1`, Netzfehler) rechnet die Plattform mit Luftlinie und einer pauschalen Geschwindigkeit, damit
Tourvorschlag und Anfahrt nie ausfallen; die Quelle steht am Ergebnis (`osrm` oder `luftlinie`).
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.disposition import ZEITZONE
from core.domaene import als_utc, protokollieren
from core.modelle import Mandant, Ressource, Termin

OSRM_DEMO = "https://router.project-osrm.org"
LUFTLINIE_KMH = 35.0          # innerörtlich realistische Durchschnittsgeschwindigkeit
LUFTLINIE_FAKTOR = 1.3        # Straßenweg ist länger als die Luftlinie
Punkt = tuple[float, float]   # (lat, lon)


def haversine_km(a: Punkt, b: Punkt) -> float:
    r = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


class Router:
    """Fahrzeitmatrix und Routen; `url=None` erzwingt Luftlinie."""

    def __init__(self, url: str | None, client: httpx.Client | None = None, profil: str = "driving"):
        self.url = url.rstrip("/") if url else None
        self.profil = profil
        self.client = client or httpx.Client(timeout=8.0, headers={"User-Agent": "shk-backoffice/0.2"})

    @classmethod
    def aus_umgebung(cls, stammdaten: dict[str, Any] | None = None) -> Router:
        if os.environ.get("ROUTING_AUS", "").lower() in ("1", "true", "ja"):
            return cls(None)
        return cls((stammdaten or {}).get("routing_url") or os.environ.get("ROUTING_URL") or OSRM_DEMO)

    def _koordinaten(self, punkte: list[Punkt]) -> str:
        return ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in punkte)

    def matrix(self, punkte: list[Punkt]) -> tuple[list[list[float]], list[list[float]], str]:
        """Minuten und Kilometer je Paar; Rückfall Luftlinie."""
        if self.url and len(punkte) >= 2:
            try:
                r = self.client.get(f"{self.url}/table/v1/{self.profil}/{self._koordinaten(punkte)}",
                                    params={"annotations": "duration,distance"})
                r.raise_for_status()
                d = r.json()
                if d.get("code") == "Ok":
                    minuten = [[(x or 0) / 60 for x in zeile] for zeile in d["durations"]]
                    km = [[(x or 0) / 1000 for x in zeile] for zeile in d["distances"]]
                    return minuten, km, "osrm"
            except (httpx.HTTPError, ValueError, KeyError):
                pass
        km = [[haversine_km(a, b) * LUFTLINIE_FAKTOR for b in punkte] for a in punkte]
        return [[x / LUFTLINIE_KMH * 60 for x in zeile] for zeile in km], km, "luftlinie"

    def route(self, punkte: list[Punkt]) -> dict[str, Any]:
        """Gesamtroute über alle Punkte: Minuten, Kilometer, Linie als [lat, lon]-Liste."""
        if self.url and len(punkte) >= 2:
            try:
                r = self.client.get(f"{self.url}/route/v1/{self.profil}/{self._koordinaten(punkte)}",
                                    params={"overview": "simplified", "geometries": "geojson"})
                r.raise_for_status()
                d = r.json()
                if d.get("code") == "Ok" and d.get("routes"):
                    rt = d["routes"][0]
                    return {"minuten": round(rt["duration"] / 60), "km": round(rt["distance"] / 1000, 1),
                            "linie": [[lat, lon] for lon, lat in rt["geometry"]["coordinates"]], "quelle": "osrm"}
            except (httpx.HTTPError, ValueError, KeyError):
                pass
        km = sum(haversine_km(a, b) for a, b in zip(punkte, punkte[1:])) * LUFTLINIE_FAKTOR
        return {"minuten": round(km / LUFTLINIE_KMH * 60), "km": round(km, 1), "linie": [list(p) for p in punkte], "quelle": "luftlinie"}


def navigation(lat: float | None, lon: float | None, adresse: str) -> dict[str, str]:
    """Links für die Navigations-App des Monteurs (geo-URI für Android, Apple Karten, OSM, Google)."""
    if lat is None or lon is None:
        q = quote(adresse)
        return {"geo": f"geo:0,0?q={q}", "apple": f"https://maps.apple.com/?q={q}",
                "osm": f"https://www.openstreetmap.org/search?query={q}", "google": f"https://www.google.com/maps/dir/?api=1&destination={q}"}
    return {"geo": f"geo:{lat},{lon}?q={lat},{lon}({quote(adresse)})", "apple": f"https://maps.apple.com/?daddr={lat},{lon}",
            "osm": f"https://www.openstreetmap.org/directions?to={lat}%2C{lon}", "google": f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}"}


def _betrieb(m: Mandant) -> Punkt | None:
    st = m.stammdaten or {}
    return (st["lat"], st["lon"]) if st.get("lat") is not None and st.get("lon") is not None else None


def _reihenfolge(matrix: list[list[float]], start: int, stopps: list[int]) -> list[int]:
    """Nächster-Nachbar-Heuristik mit 2-opt-Verbesserung; für die Tagesgröße eines Handwerksbetriebs ausreichend."""
    rest, aktuell, folge = list(stopps), start, []
    while rest:
        naechster = min(rest, key=lambda i: matrix[aktuell][i])
        folge.append(naechster)
        rest.remove(naechster)
        aktuell = naechster

    def laenge(f: list[int]) -> float:
        return sum(matrix[a][b] for a, b in zip([start] + f, f))
    besser = True
    while besser and len(folge) > 2:
        besser = False
        for i in range(len(folge) - 1):
            for j in range(i + 1, len(folge)):
                kandidat = folge[:i] + folge[i:j + 1][::-1] + folge[j + 1:]
                if laenge(kandidat) + 1e-9 < laenge(folge):
                    folge, besser = kandidat, True
    return folge


def tour(s: Session, m: Mandant, r: Ressource, tag_: datetime, router: Router) -> dict[str, Any]:
    """Tagestour eines Monteurs: Ist-Reihenfolge mit Fahrzeiten und optimierter Vorschlag, beide ab Betriebsstandort."""
    von = datetime.combine(als_utc(tag_).astimezone(ZEITZONE).date(), datetime.min.time(), ZEITZONE)
    termine = s.scalars(select(Termin).where(Termin.mandant_id == m.id, Termin.ressource_id == r.id, Termin.beginn >= von,
                                             Termin.beginn < von + timedelta(days=1), Termin.status != "abgesagt")
                        .order_by(Termin.beginn)).all()
    start = _betrieb(m)
    stopps = []
    for t in termine:
        k = t.servicefall.kunde
        adresse = ", ".join(x for x in (k.strasse, f"{k.plz or ''} {k.ort or ''}".strip()) if x)
        stopps.append({"termin_id": t.id, "servicefall_id": t.servicefall_id, "kunde_id": k.id, "kunde": k.name, "adresse": adresse,
                       "beginn": als_utc(t.beginn), "ende": als_utc(t.ende), "status": t.status, "anliegen": t.servicefall.anliegen,
                       "dringlichkeit": t.servicefall.dringlichkeit, "lat": k.lat, "lon": k.lon,
                       "navigation": navigation(k.lat, k.lon, adresse), "fahrzeit_minuten": None, "km": None})
    mit_geo = [i for i, st in enumerate(stopps) if st["lat"] is not None]
    ergebnis: dict[str, Any] = {"ressource_id": r.id, "name": r.name, "tag": von.date().isoformat(), "stopps": stopps,
                                "ohne_koordinaten": len(stopps) - len(mit_geo), "quelle": None, "linie": [],
                                "ist": {"minuten": 0, "km": 0}, "vorschlag": None}
    if start is None or not mit_geo:
        ergebnis["hinweis"] = "Betriebsstandort oder Kundenkoordinaten fehlen (Einstellungen → Stammdaten, Kunden → Geokodieren)."
        return ergebnis
    punkte: list[Punkt] = [start] + [(stopps[i]["lat"], stopps[i]["lon"]) for i in mit_geo]
    minuten, km, quelle = router.matrix(punkte)
    ergebnis["quelle"] = quelle
    vorher = 0
    for n, i in enumerate(mit_geo, 1):
        stopps[i]["fahrzeit_minuten"], stopps[i]["km"] = round(minuten[vorher][n]), round(km[vorher][n], 1)
        vorher = n
    ergebnis["ist"] = {"minuten": round(sum(minuten[a][b] for a, b in zip(range(len(punkte) - 1), range(1, len(punkte))))),
                       "km": round(sum(km[a][b] for a, b in zip(range(len(punkte) - 1), range(1, len(punkte)))), 1)}
    ergebnis["linie"] = router.route(punkte)["linie"]
    if len(mit_geo) >= 2:
        folge = _reihenfolge(minuten, 0, list(range(1, len(punkte))))
        ges_min = sum(minuten[a][b] for a, b in zip([0] + folge, folge))
        ges_km = sum(km[a][b] for a, b in zip([0] + folge, folge))
        ergebnis["vorschlag"] = {"reihenfolge": [stopps[mit_geo[n - 1]]["termin_id"] for n in folge],
                                 "minuten": round(ges_min), "km": round(ges_km, 1),
                                 "ersparnis_minuten": max(0, ergebnis["ist"]["minuten"] - round(ges_min))}
    return ergebnis


def tour_umsetzen(s: Session, m: Mandant, r: Ressource, tag_: datetime, reihenfolge: list[str], router: Router,
                  akteur_name: str | None = None) -> dict[str, Any]:
    """Termine in der gegebenen Reihenfolge neu takten: erster Termin behält seinen Beginn, danach Dauer + Fahrzeit.
    Status bleibt; eine Terminbestätigung mit neuer Uhrzeit gehört als Freigabe B ins Büro, deshalb nur vorgeschlagene
    oder bestätigte Termine desselben Tages, keine Verschiebung über die Arbeitszeit hinaus."""
    termine = {t.id: t for t in s.scalars(select(Termin).where(Termin.mandant_id == m.id, Termin.ressource_id == r.id,
                                                                Termin.id.in_(reihenfolge)))}
    if set(termine) != set(reihenfolge) or not reihenfolge:
        raise ValueError("Reihenfolge enthält unbekannte oder fremde Termine")
    folge = [termine[i] for i in reihenfolge]
    start = _betrieb(m)
    punkte: list[Punkt] = ([start] if start else []) + [(t.servicefall.kunde.lat, t.servicefall.kunde.lon) for t in folge]
    if any(p[0] is None for p in punkte):
        raise ValueError("Alle Termine der Tour brauchen Kundenkoordinaten")
    minuten, _, quelle = router.matrix(punkte)
    beginn = min(als_utc(t.beginn) for t in folge)
    tagesende = datetime.combine(beginn.astimezone(ZEITZONE).date(), r.arbeitsende, ZEITZONE)
    versatz = 1 if start else 0
    geaendert = []
    for n, t in enumerate(folge):
        dauer = als_utc(t.ende) - als_utc(t.beginn)
        if n > 0:
            beginn = beginn + timedelta(minutes=round(minuten[n - 1 + versatz][n + versatz]))
            beginn = beginn + (timedelta(minutes=5) - timedelta(minutes=beginn.minute % 5)) if beginn.minute % 5 else beginn
        if beginn + dauer > als_utc(tagesende):
            raise ValueError(f"Tour überschreitet die Arbeitszeit von {r.name} ({r.arbeitsende.strftime('%H:%M')})")
        if als_utc(t.beginn) != beginn:
            geaendert.append({"termin_id": t.id, "vorher": als_utc(t.beginn), "nachher": beginn})
        t.beginn, t.ende = beginn, beginn + dauer
        beginn = beginn + dauer
    protokollieren(s, m.id, "tour.umgesetzt", "ressource", r.id, "mensch",
                   {"tag": folge[0].beginn.astimezone(ZEITZONE).date().isoformat(), "termine": reihenfolge, "quelle": quelle,
                    "verschoben": len(geaendert)}, akteur_name)
    return {"verschoben": geaendert, "quelle": quelle}
