"""Kundenkarte, Geokodierung, Vollständigkeit der Kundendaten und Einwilligungen."""
from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from core import karte
from core.api import app_erzeugen
from core.db import Datenbank
from core.modelle import Kunde, Mandant
from core.seed import KUNDEN, demodaten_anlegen

JETZT = datetime(2026, 11, 17, 7, 0, tzinfo=timezone.utc)


def demo():
    db = Datenbank("sqlite://")
    app = app_erzeugen(db, uhr=lambda: JETZT)
    c = TestClient(app)
    with db.sitzungen() as s:
        m = demodaten_anlegen(s, JETZT)
    return c, db, app, {"X-Mandant": m}


def test_kundenkarte_liefert_punkte_einsaetze_und_verteilung():
    c, _, _, h = demo()
    k = c.get("/kunden/karte", headers=h).json()
    assert len(k["kunden"]) == len(KUNDEN)                       # alle Stammkunden mit Koordinaten
    assert [x["name"] for x in k["ohne_koordinaten"]] == ["Sabine Quast"]   # Neukunde aus dem Webformular: Adresse da, noch nicht geokodiert
    assert k["betrieb"]["lat"] and k["kacheln"]["url"].startswith("https://")
    assert all(50.7 < p["lat"] < 51.1 and 6.7 < p["lon"] < 7.2 for p in k["kunden"])
    assert k["je_ort"][0]["ort"] == "Köln" and k["je_ort"][0]["anzahl"] >= 8 and "50825" in k["je_ort"][0]["plz"]
    assert k["einsaetze_heute"] and all(e["ressource"] and e["kunde"] for e in k["einsaetze_heute"])
    dringende = [p for p in k["kunden"] if p["dringlichkeit"]]
    assert dringende and all(p["offene_vorgaenge"] >= 1 for p in dringende)


def test_vollstaendigkeit_benennt_luecken_und_verschwindet_nach_pflege():
    c, _, _, h = demo()
    v = c.get("/kunden/vollstaendigkeit", headers=h).json()
    assert v["kunden"] == len(KUNDEN) + 1 and 0 < v["vollstaendig"] < v["kunden"]
    felder = {f["feld"]: f["anzahl"] for f in v["je_feld"]}
    assert felder["email"] >= 4 and felder["geo"] == 1 and felder["telefon"] == 0
    hollmann = next(z for z in v["unvollstaendig"] if z["name"] == "Herr Hollmann")
    assert {"email", "einwilligung"} <= set(hollmann["fehlend"])
    d = c.get(f"/kunden/{hollmann['id']}", headers=h).json()
    assert d["vollstaendigkeit"]["vollstaendig"] is False
    c.patch(f"/kunden/{hollmann['id']}", headers=h, json={"email": "hollmann@example.invalid"})
    r = c.put(f"/kunden/{hollmann['id']}/einwilligungen", headers=h, json={"email": True, "sms": False})
    assert r.status_code == 200 and r.json()["einwilligungen"]["email"].startswith("2026-11-17")
    d = c.get(f"/kunden/{hollmann['id']}", headers=h).json()
    assert d["vollstaendigkeit"]["vollstaendig"] is True
    # Entzug der Einwilligung wird protokolliert und entfernt den Zeitstempel
    r = c.put(f"/kunden/{hollmann['id']}/einwilligungen", headers=h, json={"email": False})
    assert "email" not in r.json()["einwilligungen"]
    typen = [e["typ"] for e in c.get("/ereignisse", headers=h).json()]
    assert "kunde.einwilligung" in typen


def test_geokodierung_nutzt_cache_und_setzt_quelle(monkeypatch):
    c, db, app, h = demo()
    k = c.post("/kunden", headers=h, json={"name": "Neu ohne Punkt", "telefon": "0221 555", "strasse": "Domkloster 4",
                                           "plz": "50667", "ort": "Köln"}).json()
    c.post(f"/kunden/{k['id']}/objekte", headers=h, json={"strasse": "Domkloster 4", "plz": "50667", "ort": "Köln"})
    aufrufe = []

    def geocoder(adresse):
        aufrufe.append(adresse)
        return (50.9413, 6.9583) if "Domkloster" in adresse else None

    app.state.geocoder = geocoder
    e = c.post("/kunden/geokodieren", headers=h).json()
    # Kunde und Objekt mit derselben Adresse: ein Aufruf; die Neukundin aus dem Webformular (Weyertal) bleibt ohne Treffer
    assert e["gesetzt"] == 2 and len(aufrufe) == 2 and e["ohne_treffer"] == 1
    assert e["fehler"] == 0 and e["uebrig"] == 0
    d = c.get(f"/kunden/{k['id']}", headers=h).json()["kunde"]
    assert d["lat"] == 50.9413 and d["geo_quelle"] == "nominatim" and d["geo_stand"]
    # Adressänderung verwirft automatisch ermittelte Koordinaten, manuelle Koordinaten bleiben mit Quelle "manuell"
    d = c.patch(f"/kunden/{k['id']}", headers=h, json={"strasse": "Domkloster 5"}).json()
    assert d["lat"] is None and d["geo_quelle"] is None
    d = c.patch(f"/kunden/{k['id']}", headers=h, json={"lat": 50.94, "lon": 6.95}).json()
    assert d["geo_quelle"] == "manuell"
    monkeypatch.setenv("GEOCODER_AUS", "1")
    assert c.post("/kunden/geokodieren", headers=h).status_code == 409


def test_geokodierung_begrenzt_und_faengt_dienstfehler():
    c, db, app, h = demo()
    with db.sitzungen() as s:
        m = s.scalar(karte.select(Mandant))
        for k in s.scalars(karte.select(Kunde)).all():
            k.lat = k.lon = None
        s.commit()
        mandant_id = m.id

    def kaputt(adresse):
        raise httpx.ConnectError("kein Netz")

    with db.sitzungen() as s:
        m = s.get(Mandant, mandant_id)
        e = karte.geokodieren(s, m, kaputt, JETZT, limit=3)
    assert e["geprueft"] == 3 and e["fehler"] == 3 and e["uebrig"] >= 9 and e["gesetzt"] == 0


def test_nominatim_client_fragt_mit_user_agent_und_wartet():
    gesehen = {}

    def antwort(request: httpx.Request) -> httpx.Response:
        gesehen["ua"] = request.headers["user-agent"]
        gesehen["q"] = request.url.params["q"]
        return httpx.Response(200, json=[{"lat": "50.95", "lon": "6.92"}])

    client = httpx.Client(transport=httpx.MockTransport(antwort))
    g = karte.nominatim(url="https://geo.example.invalid/search", email="buero@example.invalid", pause=0, client=client)
    assert g("Werkstattstraße 12, 50823 Köln, Deutschland") == (50.95, 6.92)
    assert "shk-backoffice" in gesehen["ua"] and "Werkstattstraße" in gesehen["q"]
    assert karte.adresse_text("", "50823", "Köln") is None and karte.adresse_text("A 1", None, "Köln") == "A 1, Köln, Deutschland"
