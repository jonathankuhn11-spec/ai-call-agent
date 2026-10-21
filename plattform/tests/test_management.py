from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from core.api import app_erzeugen
from core.db import Datenbank
from core.seed import DEMO_NAME, demodaten_anlegen

BERLIN = ZoneInfo("Europe/Berlin")
JETZT = datetime(2026, 11, 17, 7, 0, tzinfo=timezone.utc)  # Dienstag, 08:00 Berlin


def demo():
    db = Datenbank("sqlite://")
    c = TestClient(app_erzeugen(db, uhr=lambda: JETZT))
    with db.sitzungen() as s:
        m = demodaten_anlegen(s, JETZT)
    return c, db, m, {"X-Mandant": m}


def test_seed_ist_idempotent_und_vollstaendig():
    c, db, m, h = demo()
    with db.sitzungen() as s:
        assert demodaten_anlegen(s, JETZT) == m
    assert [x["name"] for x in c.get("/mandanten").json()] == [DEMO_NAME]
    assert len(c.get("/kunden", headers=h).json()) == 12
    assert len(c.get("/freigaben", headers=h).json()) == 3   # 2 Termine (B) + 1 Rechnung (C)


def test_termine_behalten_ortszeit_auch_in_sqlite():
    c, _, _, h = demo()
    p = c.get("/plantafel", headers=h, params={"tag": "2026-11-17", "tage": 1}).json()
    arndt = next(r for r in p["ressourcen"] if r["name"].startswith("Monteur Arndt"))
    assert datetime.fromisoformat(arndt["termine"][0]["beginn"]).astimezone(BERLIN).hour == 9


def test_wartungsplanung_legt_faelle_und_freigaben_an_und_ist_wiederholbar():
    c, _, _, h = demo()
    vorher = len(c.get("/freigaben", headers=h).json())
    r = c.post("/wartung/planen", headers=h).json()
    assert r["angelegt"] >= 5 and all(f["anlage"] for f in r["servicefaelle"])
    assert len(c.get("/freigaben", headers=h).json()) == vorher + r["angelegt"]
    assert c.post("/wartung/planen", headers=h).json()["angelegt"] == 0


def test_kennzahlen_und_reaktionszeit():
    c, _, _, h = demo()
    k = c.get("/kennzahlen", headers=h).json()
    assert k["offene_vorgaenge"] == 8 and k["anrufe_erfasst"] == 6 and k["median_stunden_bis_einplanung"] > 0


def test_sparql_nur_lesend():
    c, _, _, h = demo()
    ok = c.post("/ontologie/sparql", headers=h, json={"abfrage": "ASK { ?s ?p ?o }"}).json()
    assert ok == {"typ": "ask", "ergebnis": True}
    schreibend = c.post("/ontologie/sparql", headers=h, json={"abfrage": "INSERT DATA { <urn:a> <urn:b> <urn:c> }"})
    assert schreibend.status_code == 422
    kaputt = c.post("/ontologie/sparql", headers=h, json={"abfrage": "SELECT ?x WHERE {"})
    assert kaputt.status_code == 422


def test_oberflaeche_wird_ausgeliefert():
    c, _, _, _ = demo()
    assert c.get("/", follow_redirects=False).headers["location"] == "/app/"
    seite = c.get("/app/")
    assert seite.status_code == 200 and "SHK-Backoffice" in seite.text
    assert c.get("/app/app.js").status_code == 200


def test_voice_bietet_unterschiedliche_uhrzeiten_an():
    c, _, _, h = demo()
    r = c.post("/voice/zeitfenster", headers=h, json={"anliegen": "Wasserhahn tropft", "anzahl": 3}).json()
    beginne = [z["beginn"] for z in r["zeitfenster"]]
    assert len(beginne) == 3 and len(set(beginne)) == 3
