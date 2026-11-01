"""Referenzfluss der Architektur als ausführbare Demo: Heizungsausfall per Telefon bis Outbox.

Läuft vollständig im Prozess (SQLite im Speicher, feste Uhr), ohne Telefonie und ohne LLM:
die Voice-Tool-Aufrufe sind genau die, die der Voice-Agent im Gespräch absetzt.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from rdflib import Graph

from core.api import app_erzeugen
from core.db import Datenbank
from core.zustellung import ProtokollAdapter, abarbeiten

DEMO_ZEIT = datetime(2026, 11, 17, 8, 0, tzinfo=ZoneInfo("Europe/Berlin"))  # Dienstag in der Heizperiode


def demo_client(db: Datenbank | None = None) -> tuple[TestClient, Datenbank]:
    db = db or Datenbank("sqlite://")
    return TestClient(app_erzeugen(db, uhr=lambda: DEMO_ZEIT)), db


def stammdaten_anlegen(c: TestClient) -> dict[str, str]:
    m = c.post("/mandanten", json={"name": "Demo Haustechnik GmbH", "policies": {}}).json()
    h = {"X-Mandant": m["id"]}
    gas = c.post("/ressourcen", headers=h, json={"name": "Monteur A", "qualifikationen": ["gas", "trinkwasser"]}).json()
    kaelte = c.post("/ressourcen", headers=h, json={"name": "Monteur B", "qualifikationen": ["kaeltetechnik", "elektro"]}).json()
    k = c.post("/kunden", headers=h, json={"name": "Familie Beispiel", "telefon": "0221 1234567", "ort": "Köln",
                                           "vertragsstatus": "wartungsvertrag"}).json()
    o = c.post(f"/kunden/{k['id']}/objekte", headers=h, json={"ort": "Köln", "gebaeudetyp": "Einfamilienhaus"}).json()
    a = c.post(f"/objekte/{o['id']}/anlagen", headers=h, json={"anlagentyp": "gas_brennwert", "hersteller": "Viessmann",
                                                               "baujahr": 2012, "letzte_wartung": "2025-09-10"}).json()
    return {"mandant": m["id"], "monteur_gas": gas["id"], "monteur_kaelte": kaelte["id"], "kunde": k["id"], "anlage": a["id"]}


def referenzfluss() -> dict[str, Any]:
    c, db = demo_client()
    ids = stammdaten_anlegen(c)
    h = {"X-Mandant": ids["mandant"]}

    # Gespräch: Agent erkennt den Anrufer, fragt Zeitfenster ab, legt Servicefall mit Wunschtermin an
    kunde = c.post("/voice/kunde-suchen", headers=h, json={"telefon": "+492211234567"}).json()
    fenster = c.post("/voice/zeitfenster", headers=h, json={"anliegen": "Heizung ist aus, Therme zeigt Störung",
                                                            "anlagentyp": "gas_brennwert"}).json()
    gewaehlt = fenster["zeitfenster"][0]
    fall = c.post("/voice/servicefall", headers=h, json={
        "telefon": "0221 1234567", "name": kunde["name"], "anliegen": "Heizung ist aus, Therme zeigt Störung",
        "anlagentyp": "gas_brennwert", "ressource_id": gewaehlt["ressource_id"], "beginn": gewaehlt["beginn"]}).json()
    c.post("/voice/gespraech-beendet", headers=h, json={"telefon": "0221 1234567", "servicefall_id": fall["servicefall_id"],
                                                        "transkript": "…", "zusammenfassung": "Heizungsausfall, Termin vorgemerkt"})

    # Büro: Freigabe-Inbox abarbeiten (Klasse B), danach Outbox zustellen
    offen = c.get("/freigaben", headers=h).json()
    for f in offen:
        c.post(f"/freigaben/{f['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "buero"})
    adapter = {z: ProtokollAdapter(z) for z in ("fachsoftware", "endkunde", "finanz")}
    with db.sitzungen() as s:
        zustellung = abarbeiten(s, adapter, DEMO_ZEIT)

    detail = c.get(f"/servicefaelle/{fall['servicefall_id']}", headers=h).json()
    return {
        "kunde_erkannt": kunde["gefunden"],
        "dringlichkeit": fenster["dringlichkeit"],
        "angebotene_zeitfenster": [(z["monteur"], z["beginn"]) for z in fenster["zeitfenster"]],
        "termin_vor_freigabe_verbindlich": fall["termin"]["verbindlich"],
        "freigaben_bearbeitet": len(offen),
        "status_servicefall": detail["servicefall"]["status"],
        "ereignisse": [e["typ"] for e in detail["ereignisse"]],
        "zustellung": zustellung,
        "zwilling_betrieb": c.get("/twin/betrieb", headers=h).json()["vorgaenge_je_status"],
        "ontologie_tripel": len(Graph().parse(data=c.get("/ontologie/export", headers=h).text, format="turtle")),
    }
