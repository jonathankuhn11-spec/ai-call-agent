"""Neue Open-Source-Bausteine: Touren (OSRM/Luftlinie), Kalender-Abo, Feiertage, GiroCode, Push (ntfy), Metriken,
Rufnummern- und IBAN-Prüfung, SPARQL mit Inferenz."""
from datetime import datetime, timezone

import io

import httpx
from fastapi.testclient import TestClient
from pypdf import PdfReader

from core import kalender, routing
from core.adapter import Register
from core.adapter.ntfy import NtfyAdapter
from core.api import app_erzeugen
from core.db import Datenbank
from core.modelle import Mandant, OutboxEintrag
from core.regeln import telefon_gueltig, telefon_normalisieren
from core.seed import demodaten_anlegen
from core.zustellung import abarbeiten

JETZT = datetime(2026, 11, 17, 7, 0, tzinfo=timezone.utc)   # Dienstag, 08:00 Berlin


def demo(uhr=JETZT):
    db = Datenbank("sqlite://")
    app = app_erzeugen(db, uhr=lambda: uhr)
    app.state.router = routing.Router(None)
    c = TestClient(app)
    with db.sitzungen() as s:
        m = demodaten_anlegen(s, uhr)
    return c, db, app, {"X-Mandant": m}


# --- Touren ---------------------------------------------------------------------------------------
def test_tour_liefert_fahrzeiten_vorschlag_und_umsetzung():
    c, db, app, h = demo()
    t = c.get("/tour", headers=h).json()
    assert t["tag"] == "2026-11-17" and len(t["touren"]) == 3
    tour = next(x for x in t["touren"] if x["stopps"])
    assert tour["quelle"] == "luftlinie" and all(st["fahrzeit_minuten"] is not None for st in tour["stopps"] if st["lat"])
    assert tour["linie"] and tour["stopps"][0]["navigation"]["geo"].startswith("geo:")
    # zweiten Termin am selben Tag für denselben Monteur anlegen, dann Vorschlag und Umsetzung
    f = c.post("/servicefaelle", headers=h, json={"kunde_id": c.get("/kunden?q=Kremer", headers=h).json()[0]["id"],
                                                  "anliegen": "Wartung Gastherme"}).json()
    r = c.post(f"/servicefaelle/{f['id']}/termine", headers=h, json={"ressource_id": tour["ressource_id"], "beginn": "2026-11-17T13:00:00+01:00",
                                                                     "ende": "2026-11-17T14:00:00+01:00"})
    assert r.status_code in (200, 201), r.text
    tour = next(x for x in c.get(f"/tour?ressource_id={tour['ressource_id']}", headers=h).json()["touren"])
    assert len(tour["stopps"]) == 2 and tour["vorschlag"] and len(tour["vorschlag"]["reihenfolge"]) == 2
    assert tour["vorschlag"]["minuten"] <= tour["ist"]["minuten"]
    umgekehrt = list(reversed([st["termin_id"] for st in tour["stopps"]]))
    e = c.post("/tour/umsetzen", headers=h, json={"ressource_id": tour["ressource_id"], "tag": "2026-11-17", "reihenfolge": umgekehrt})
    assert e.status_code == 200, e.text
    assert len(e.json()["verschoben"]) == 2
    neu = next(x for x in c.get(f"/tour?ressource_id={tour['ressource_id']}", headers=h).json()["touren"])
    assert [st["termin_id"] for st in neu["stopps"]] == umgekehrt
    assert "tour.umgesetzt" in [x["typ"] for x in c.get("/ereignisse", headers=h).json()]


def test_router_osrm_und_rueckfall():
    def osrm(request: httpx.Request) -> httpx.Response:
        if "/table/" in request.url.path:
            return httpx.Response(200, json={"code": "Ok", "durations": [[0, 600], [660, 0]], "distances": [[0, 7000], [7500, 0]]})
        return httpx.Response(200, json={"code": "Ok", "routes": [{"duration": 600, "distance": 7000,
                                                                  "geometry": {"coordinates": [[6.92, 50.95], [6.97, 50.90]]}}]})
    r = routing.Router("https://osrm.example.invalid", client=httpx.Client(transport=httpx.MockTransport(osrm)))
    minuten, km, quelle = r.matrix([(50.95, 6.92), (50.90, 6.97)])
    assert quelle == "osrm" and minuten[0][1] == 10 and km[1][0] == 7.5
    assert r.route([(50.95, 6.92), (50.90, 6.97)])["linie"] == [[50.95, 6.92], [50.90, 6.97]]
    kaputt = routing.Router("https://osrm.example.invalid", client=httpx.Client(transport=httpx.MockTransport(lambda rq: httpx.Response(503))))
    assert kaputt.matrix([(50.95, 6.92), (50.90, 6.97)])[2] == "luftlinie"
    assert routing._reihenfolge([[0, 10, 1], [10, 0, 10], [1, 10, 0]], 0, [1, 2]) == [2, 1]


# --- Kalender -------------------------------------------------------------------------------------
def test_kalender_abo_mit_token():
    c, db, app, h = demo()
    links = c.get("/kalender/links", headers=h).json()
    url = links["monteure"][0]["url"].split("http://localhost:8100", 1)[1]
    r = c.get(url)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    ics = r.text
    assert "BEGIN:VCALENDAR" in ics and "BEGIN:VEVENT" in ics and "X-WR-CALNAME" in ics and "GEO:" in ics
    assert "TRIGGER:-PT30M" in ics
    assert c.get(url.replace("token=", "token=0")).status_code == 404
    betrieb = c.get(links["betrieb"].split("http://localhost:8100", 1)[1]).text
    assert betrieb.count("BEGIN:VEVENT") >= ics.count("BEGIN:VEVENT") and "Bereitschaft" in betrieb
    assert kalender.token("ressource:x") != kalender.token("ressource:y")


# --- Feiertage ------------------------------------------------------------------------------------
def test_feiertage_sperren_disposition_und_erscheinen_in_plantafel_und_lohnexport():
    c, db, app, h = demo(datetime(2026, 6, 3, 7, 0, tzinfo=timezone.utc))   # Mittwoch vor Fronleichnam (NW)
    fenster = c.get("/disposition/zeitfenster?tage=3&dauer_minuten=60", headers=h).json()
    assert fenster and all(not z["beginn"].startswith("2026-06-04") for z in fenster)
    p = c.get("/plantafel?tag=2026-06-01&tage=5", headers=h).json()
    assert p["feiertage"] == {"2026-06-04": "Fronleichnam"}
    c.put("/betrieb/stammdaten", headers=h, json={"bundesland": "BE"})
    assert c.get("/plantafel?tag=2026-06-01&tage=5", headers=h).json()["feiertage"] == {}
    assert c.put("/betrieb/stammdaten", headers=h, json={"bundesland": "XX"}).status_code == 422
    c.put("/betrieb/stammdaten", headers=h, json={"bundesland": "nw"})
    rid = c.get("/ressourcen", headers=h).json()[0]["id"]
    c.post("/zeiten", headers=h, json={"ressource_id": rid, "art": "arbeit", "beginn": "2026-06-04T08:00:00+02:00", "ende": "2026-06-04T10:00:00+02:00"})
    csv = c.get("/zeiten/lohnexport?monat=2026-06", headers=h).text
    assert "davon Feiertag (min)" in csv
    zeile = next(z for z in csv.splitlines() if "2026-06-04" in z)
    assert zeile.rstrip().endswith(";120")


# --- GiroCode -------------------------------------------------------------------------------------
def test_girocode_auf_rechnung_und_mahnung():
    c, db, app, h = demo()
    with db.sitzungen() as s:
        m = s.scalar(routing.select(Mandant))
        mid = m.id
    r = c.put("/betrieb/stammdaten", headers=h, json={"iban": "DE02120300000000202051", "bic": ""})
    assert r.json()["iban"] == "DE02 1203 0000 0000 2020 51" and r.json()["bic"] == "BYLADEM1001"
    from core.rechnung import girocode
    from decimal import Decimal
    with db.sitzungen() as s:
        m = s.get(Mandant, mid)
        png = girocode(m, Decimal("123.45"), "Rechnung RE-2026-00001")
        assert png and png[:8] == b"\x89PNG\r\n\x1a\n"
        m.stammdaten = {**m.stammdaten, "iban": "DE00 ungültig"}
        assert girocode(m, Decimal("1"), "x") is None
    assert c.put("/betrieb/stammdaten", headers=h, json={"iban": "DE00123"}).status_code == 422
    # Rechnungs-PDF der Demodaten trägt den GiroCode als Bild, das Storno nicht
    c.put("/betrieb/stammdaten", headers=h, json={"iban": "DE02120300000000202051"})
    rechnungen = c.get("/rechnungen", headers=h).json()
    r = next(x for x in rechnungen if x["art"] == "rechnung")
    pdf = PdfReader(io.BytesIO(c.get(f"/rechnungen/{r['id']}/pdf", headers=h).content))
    assert any(p.images for p in pdf.pages)


# --- Push (ntfy) ----------------------------------------------------------------------------------
def test_push_hinweise_nur_mit_kanal_und_ueber_ntfy():
    c, db, app, h = demo()
    kid = c.get("/kunden", headers=h).json()[0]["id"]
    c.post("/servicefaelle", headers=h, json={"kunde_id": kid, "anliegen": "Gasgeruch im Keller", "dringlichkeit": "notdienst"})
    with db.sitzungen() as s:
        assert s.scalar(routing.select(OutboxEintrag).where(OutboxEintrag.ziel == "inhaber")) is None   # kein Kanal: kein Eintrag
    r = c.put("/integrationen/inhaber", headers=h, json={"kanaele": ["ntfy"], "ntfy": {"basis_url": "https://ntfy.example.invalid", "topic": "shk-demo", "token": "tk_1"}})
    assert r.status_code == 200, r.text
    assert c.get("/integrationen", headers=h).json()["ziele"]["inhaber"]["ntfy"]["token"] == "••••"
    c.post("/servicefaelle", headers=h, json={"kunde_id": kid, "anliegen": "Wasserrohrbruch", "dringlichkeit": "notdienst"})
    c.post("/freigaben", headers=h, json={"aktion": "rechnung_erstellen", "payload": {"kunde_id": kid, "positionen": [{"bezeichnung": "x", "menge": 1, "einheit": "Stk", "einzelpreis_netto": "10"}]}})
    gesendet = []

    def ntfy(request: httpx.Request) -> httpx.Response:
        gesendet.append((request.url.path, dict(request.headers), request.content.decode()))
        return httpx.Response(200, json={"id": "abc123"})

    with db.sitzungen() as s:
        reg = Register(zusatz={"transport": httpx.MockTransport(ntfy)})
        z = abarbeiten(s, reg)
        eintraege = s.scalars(routing.select(OutboxEintrag).where(OutboxEintrag.ziel == "inhaber")).all()
    arten = sorted(e.payload["art"] for e in eintraege)
    assert arten == ["klasse_c", "notdienst"] and all(e.status == "zugestellt" for e in eintraege)
    assert z["zugestellt"] >= 2
    pfade = {p for p, _, _ in gesendet}
    assert pfade == {"/shk-demo"}
    kopf = gesendet[0][1]
    assert kopf["authorization"] == "Bearer tk_1" and kopf["priority"] in ("4", "5")
    assert any("Wasserrohrbruch" in inhalt for _, _, inhalt in gesendet)
    # Adapter verlangt ein Topic
    try:
        NtfyAdapter({})
        raise AssertionError("topic fehlt sollte abgewiesen werden")
    except Exception as e:  # noqa: BLE001
        assert "topic" in str(e)


# --- Metriken -------------------------------------------------------------------------------------
def test_metriken_endpunkt_liefert_fachkennzahlen_und_http_zaehler():
    c, db, app, h = demo()
    c.get("/kunden", headers=h)
    text = c.get("/metrics", headers=h).text
    assert "shk_http_requests_total" in text and 'pfad="/kunden"' in text
    assert "shk_freigaben_offen{" in text and "shk_vorgaenge_offen{" in text and "shk_outbox{" in text
    assert 'klasse="C"' in text


# --- Rufnummern, SPARQL-Inferenz ------------------------------------------------------------------
def test_rufnummern_mit_libphonenumber():
    assert telefon_normalisieren("0221 / 99 01 01") == "+49221990101"
    assert telefon_normalisieren("+43 1 2345678") == "+4312345678"
    assert telefon_normalisieren("0049 176 12345678") == "+4917612345678"
    assert telefon_normalisieren("abc") is None
    assert telefon_gueltig("+49221990101") and not telefon_gueltig("+49 1") and not telefon_gueltig(None)


def test_sparql_mit_rdfs_inferenz_findet_unterklassen():
    c, db, app, h = demo()
    abfrage = """PREFIX shk: <https://w3id.org/shk-backoffice/ont#>
SELECT (COUNT(?a) AS ?n) WHERE { ?a a shk:Anlage . }"""
    ohne = c.post("/ontologie/sparql", headers=h, json={"abfrage": abfrage}).json()["zeilen"][0][0]
    mit = c.post("/ontologie/sparql", headers=h, json={"abfrage": abfrage, "inferenz": True}).json()["zeilen"][0][0]
    assert int(ohne) == 0 < int(mit)
