"""Runde 3: DATEV-Datenservices, Kundenportal, Großhandel (DATANORM, IDS-Connect, Lager), Checklisten, Suche, Förderung."""
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import select

from core import checklisten, foerderung, grosshandel, portal
from core.adapter import Register
from core.adapter.datev import DatevAdapter, zustand, zustand_pruefen
from core.adapter.email import EmailAdapter
from core.demo import DEMO_ZEIT
from core.modelle import Bestand, Ereignis, Freigabe, Kommunikation, Mandant, OutboxEintrag, Termin
from core.zustellung import abarbeiten
from tests.test_einsatz import fall_mit_termin
from tests.test_finanz import STAMMDATEN, rechnung_stellen
from tests.test_finanz import betrieb  # noqa: F401, F811

DATANORM4 = ("V;231024;Preisliste Herbst;;04;Demo-Grosshandel;\r\n"
             "A;N;UPM3-2570;0;Umwälzpumpe UPM3;25-70 130 mm;2;0;Stck;18990;01;0815;\r\n"
             "A;N;DICHT-34;0;Flachdichtung 3/4\";;1;2;Stck;1250;01;0815;\r\n"
             "P;N;DICHT-34;2;650;;;;;;\r\n"
             "A;L;ALT-001;0;Auslaufartikel;;2;0;Stck;100;01;0815;\r\n").encode("cp850")
DATANORM5 = b"""<?xml version="1.0" encoding="UTF-8"?><DATANORM><ARTIKEL><ARTNR>WP-FILTER</ARTNR><KTEXT1>Schmutzfaenger</KTEXT1><KTEXT2>1 Zoll</KTEXT2>
<MEINH>Stck</MEINH><PREIS>24.50</PREIS><PREISKZ>2</PREISKZ><WG>0900</WG></ARTIKEL></DATANORM>"""


# --- Großhandel ---------------------------------------------------------------------------------
def test_datanorm_import_trocken_und_echt(umgebung):
    c, db, ids, h = umgebung
    c.put("/betrieb/stammdaten", headers=h, json={**STAMMDATEN, "materialaufschlag_prozent": 20})
    trocken = c.post("/artikel/datanorm", headers=h, files={"datei": ("preise.001", DATANORM4)}, data={"lieferant": "Demo-Großhandel", "trocken": "true"}).json()
    assert trocken["trocken"] and trocken["gelesen"] == 3 and trocken["neu"] == 2 and trocken["deaktiviert"] == 0
    assert c.get("/artikel?alle=true", headers=h).json() == []   # Trockenlauf speichert nichts
    echt = c.post("/artikel/datanorm", headers=h, files={"datei": ("preise.001", DATANORM4)}, data={"lieferant": "Demo-Großhandel"}).json()
    assert echt["neu"] == 2 and not echt["trocken"]
    artikel = {a["nummer"]: a for a in c.get("/artikel?alle=true", headers=h).json()}
    assert artikel["UPM3-2570"]["einkauf_netto"] == "189.90" and artikel["UPM3-2570"]["verkauf_netto"] == "227.88"
    assert artikel["UPM3-2570"]["lieferant"] == "Demo-Großhandel" and artikel["UPM3-2570"]["bezeichnung"] == "Umwälzpumpe UPM3 25-70 130 mm"
    assert artikel["DICHT-34"]["einkauf_netto"] == "6.50"   # P-Satz überschreibt den Hauptsatz
    # DATANORM 5 (XML), expliziter Aufschlag
    x = c.post("/artikel/datanorm", headers=h, files={"datei": ("preise.xml", DATANORM5)}, data={"lieferant": "Demo-Großhandel", "aufschlag_prozent": "10"}).json()
    assert x["neu"] == 1
    assert {a["nummer"]: a["verkauf_netto"] for a in c.get("/artikel", headers=h).json()}["WP-FILTER"] == "26.95"
    assert c.post("/artikel/datanorm", headers=h, files={"datei": ("kaputt.xml", b"<nicht")}, data={"lieferant": "x"}).status_code == 422


def test_lager_buchen_bestellvorschlag_rapport_abbuchung_und_ids_warenkorb(umgebung):
    c, db, ids, h = umgebung
    c.put("/betrieb/stammdaten", headers=h, json={**STAMMDATEN, "ids_shop_url": "https://shop.example.invalid/ids"})
    a = c.post("/artikel", headers=h, json={"nummer": "M-210", "bezeichnung": "Dichtung 3/4\"", "einkauf_netto": 0.5, "verkauf_netto": 1.2, "lieferant": "Demo-Großhandel"}).json()
    b = c.post("/artikel", headers=h, json={"nummer": "M-300", "bezeichnung": "Entlüfter", "einkauf_netto": 4, "verkauf_netto": 9, "lieferant": "Anderer"}).json()
    assert c.post("/lager/buchen", headers=h, json={"artikel_id": a["id"], "lager": "zentral", "menge": 20, "mindestbestand": 10, "grund": "Wareneingang"}).status_code == 200
    assert c.post("/lager/buchen", headers=h, json={"artikel_id": a["id"], "lager": ids["monteur_gas"], "menge": 3, "mindestbestand": 5}).status_code == 200
    assert c.post("/lager/buchen", headers=h, json={"artikel_id": b["id"], "lager": "zentral", "menge": 0, "mindestbestand": 2}).status_code == 200
    assert c.post("/lager/buchen", headers=h, json={"artikel_id": a["id"], "lager": "gibtsnicht", "menge": 1}).status_code == 422
    bestand = c.get("/lager", headers=h).json()
    assert [(z["lagername"], z["menge"], z["unter_minimum"]) for z in bestand] == [("Fahrzeug Monteur A", 3.0, True), ("Zentrallager", 20.0, False), ("Zentrallager", 0.0, True)]
    v = c.get("/lager/bestellvorschlag", headers=h).json()
    assert v["positionen"] == 2 and [g["lieferant"] for g in v["gruppen"]] == ["Anderer", "Demo-Großhandel"]
    assert v["gruppen"][1]["positionen"][0]["menge"] == 2.0 and v["gruppen"][1]["summe_einkauf"] == 1.0
    # Rapport mit Artikelnummer bucht vom Fahrzeuglager ab; Freitext bleibt unberührt
    fall_id, termin_id = fall_mit_termin(c, h, ids, ids["monteur_gas"])
    r = c.post(f"/servicefaelle/{fall_id}/rapport", headers=h, json={"taetigkeiten": "Dichtung erneuert", "ressource_id": ids["monteur_gas"],
                                                                   "material": [{"menge": "2", "nummer": "M-210", "bezeichnung": "Dichtung"}, {"menge": "1", "bezeichnung": "Kleinteile"}]})
    assert r.status_code == 201, r.text
    assert r.json()["messwerte"]["lagerbuchungen"] == [{"artikel_id": a["id"], "nummer": "M-210", "lager": ids["monteur_gas"], "menge": -2.0, "bestand": 1.0}]
    with db.sitzungen() as s:
        assert s.scalar(select(Bestand).where(Bestand.lager == ids["monteur_gas"])).menge == 1.0
        assert s.scalar(select(Ereignis).where(Ereignis.typ == "lager.gebucht", Ereignis.payload["grund"].as_string().like("Rapport%"))) is not None
    csv_ = c.get("/lager/export.csv", headers=h)
    assert csv_.status_code == 200 and "Fahrzeug Monteur A;M-210" in csv_.text
    # IDS-Connect: Formular mit Warenkorb-XML an die Shop-Adresse
    html = c.post("/lager/warenkorb", headers=h, json={"positionen": v["gruppen"][1]["positionen"]}).text
    assert 'action="https://shop.example.invalid/ids"' in html and 'name="action" value="WARENKORB"' in html and "&lt;ARTIKEL_NR&gt;M-210&lt;/ARTIKEL_NR&gt;" in html
    assert c.post("/lager/warenkorb", headers=h, json={"positionen": [{"nummer": "x"}], "shop_url": "http://unsicher"}).status_code == 422


# --- Checklisten -----------------------------------------------------------------------------------
def test_checkliste_befunde_im_rapport(umgebung):
    c, db, ids, h = umgebung
    liste = c.get("/checklisten/gas_brennwert", headers=h).json()
    assert liste["bezeichnung"] == "Gas-Brennwertkessel" and any(p["schluessel"] == "abgas_co" and p["max"] == 400 for p in liste["punkte"])
    assert c.get("/checklisten/unbekannt", headers=h).status_code == 422
    assert len(c.get("/checklisten", headers=h).json()) == 8
    fall = c.post("/servicefaelle", headers=h, json={"kunde_id": ids["kunde"], "anlage_id": ids["anlage"], "anliegen": "Wartung"}).json()
    t = c.post(f"/servicefaelle/{fall['id']}/termine", headers=h, json={"ressource_id": ids["monteur_gas"], "beginn": DEMO_ZEIT.replace(hour=9).isoformat()}).json()
    c.post(f"/freigaben/{t['freigabe']['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "buero"})
    r = c.post(f"/servicefaelle/{fall['id']}/rapport", headers=h, json={"taetigkeiten": "Wartung nach Checkliste", "ressource_id": ids["monteur_gas"],
                                                                      "messwerte": {"checkliste": {"abgas_co": "520", "gasdichtheit": "ja", "sichtpruefung": "ja", "brenner_reinigung": "ja",
                                                                                                  "kondensat": "ja", "mag": "ja", "sicherheitsventil": "nein", "anlagendruck": "1,6"}}}).json()
    bef = r["messwerte"]["befunde"]
    assert bef["gesamt"] == "auffaellig" and bef["auffaellig"] == 2 and bef["offen"] == 0
    auff = {b["schluessel"]: b for b in bef["befunde"] if b["befund"] == "auffaellig"}
    assert set(auff) == {"abgas_co", "sicherheitsventil"} and auff["abgas_co"]["grenze"].startswith("–400")
    assert r["hinweise_kunde"].startswith("Auffällig: CO unverdünnt im Abgas: 520 ppm")
    assert checklisten.auswerten("waermepumpe", {})["gesamt"] == "unvollstaendig"


# --- Suche -------------------------------------------------------------------------------------------
def test_globale_suche_nach_rechten(umgebung):
    c, db, ids, h = umgebung
    rechnung_stellen(c, h, ids, betrag_netto=100)
    c.post("/artikel", headers=h, json={"nummer": "A-1", "bezeichnung": "Thermostatkopf", "verkauf_netto": 19})
    c.post("/servicefaelle", headers=h, json={"kunde_id": ids["kunde"], "anliegen": "Thermostat klemmt"})
    t = c.get("/suche?q=Thermostat", headers=h).json()["treffer"]
    assert sorted(x["typ"] for x in t) == ["artikel", "servicefall"]
    t = c.get("/suche?q=Beispiel", headers=h).json()["treffer"]
    assert {x["typ"] for x in t} >= {"kunde", "servicefall", "rechnung"}
    assert c.get("/suche?q=1234567", headers=h).json()["treffer"][0]["typ"] == "kunde"   # Rufnummernsuche
    assert c.get("/suche?q=x", headers=h).status_code == 422


# --- Förderung -----------------------------------------------------------------------------------------
def test_foerderrechner():
    e = foerderung.heizung(32000, "waermepumpe", 1, True, True, True, True)
    assert e["foerdersatz_prozent"] == 70 and e["gedeckelt_auf_maximum"] and e["foerderfaehige_kosten"] == 30000 and e["zuschuss_gesamt"] == 21000
    e = foerderung.heizung(60000, "biomasse", 3, False, True, True, biomasse_staubarm=True)   # Vermieter: nur Grundförderung, Deckel 60.000
    assert e["foerdersatz_prozent"] == 30 and e["kostendeckel"] == 60000 and e["zuschuss"] == 18000 and e["zuschuss_gesamt"] == 20500
    e = foerderung.einzelmassnahme(20000, 1, True)
    assert e["foerdersatz_prozent"] == 20 and e["zuschuss"] == 4000
    assert "30.000,00 €" in foerderung.angebotstext(foerderung.heizung(32000, "waermepumpe"))


def test_foerderrechner_endpunkt(umgebung):
    c, db, ids, h = umgebung
    r = c.post("/foerderung/rechner", headers=h, json={"programm": "heizung", "kosten": 25000, "heizungsart": "waermepumpe", "alte_heizung_ersetzt": True, "natuerliches_kaeltemittel": True})
    assert r.status_code == 200 and r.json()["foerdersatz_prozent"] == 55 and r.json()["angebotstext"].startswith("Voraussichtliche Förderung")
    assert c.post("/foerderung/rechner", headers=h, json={"kosten": 1000, "heizungsart": "kohle"}).status_code == 422


# --- Kundenportal ----------------------------------------------------------------------------------------
def test_portal_bestaetigung_absage_erinnerung_und_links(betrieb):  # noqa: F811
    c, db, ids, h = betrieb
    c.put("/betrieb/stammdaten", headers=h, json={**STAMMDATEN, "basis_url": "https://shk.example.test"})
    fall = c.post("/servicefaelle", headers=h, json={"kunde_id": ids["kunde"], "anliegen": "Heizung aus"}).json()
    t = c.post(f"/servicefaelle/{fall['id']}/termine", headers=h, json={"ressource_id": ids["monteur_gas"], "beginn": (DEMO_ZEIT + timedelta(days=1)).replace(hour=10).isoformat()}).json()
    c.post(f"/freigaben/{t['freigabe']['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "buero"})
    link = c.get(f"/servicefaelle/{fall['id']}/portal", headers=h).json()
    assert link["link"] == f"https://shk.example.test/portal/#{fall['id']}/{portal.token(fall['id'])}"
    tok = portal.token(fall["id"])
    assert c.get(f"/portal/faelle/{fall['id']}?token=falsch").status_code == 404
    assert c.get("/portal/").status_code == 200 and "Ihr Termin" in c.get("/portal/").text
    d = c.get(f"/portal/faelle/{fall['id']}?token={tok}").json()
    assert d["betrieb"]["name"] == "Demo Haustechnik GmbH" and d["termine"][0]["status"] == "bestaetigt" and d["termine"][0]["monteur"] == "Monteur A"
    # Terminbestätigung per E-Mail enthält den Portal-Link
    gesendet = []
    with db.sitzungen() as s:
        abarbeiten(s, {"endkunde": EmailAdapter({"absender": "buero@example.test", "host": "x"}, versand=gesendet.append)}, DEMO_ZEIT)
    assert any(link["link"] in n.get_content() for n in gesendet if n["Subject"].startswith("Terminbestätigung"))
    # Erinnerung im Tageslauf (Termin morgen) mit Portal-Link, idempotent
    assert c.post("/tagesaufgaben", headers=h).json()["terminerinnerungen"] == 1
    assert c.post("/tagesaufgaben", headers=h).json()["terminerinnerungen"] == 0
    with db.sitzungen() as s:
        e = s.scalar(select(OutboxEintrag).where(OutboxEintrag.aktion == "nachricht", OutboxEintrag.idempotenz_schluessel.like("erinnerung:%")))
        assert e is not None and "Terminerinnerung" in e.payload["betreff"] and link["link"] in e.payload["text"]
    # Bestätigung im Portal: Kommunikation + Ereignis; dann Absage: Termin abgesagt, Vorgang zurück auf qualifiziert
    r = c.post(f"/portal/faelle/{fall['id']}/termine/{t['termin']['id']}?token={tok}", json={"aktion": "bestaetigen"})
    assert r.status_code == 200 and "bestätigt" in r.json()["hinweis"]
    r = c.post(f"/portal/faelle/{fall['id']}/termine/{t['termin']['id']}?token={tok}", json={"aktion": "absagen", "grund": "Bin verreist"})
    assert r.status_code == 200, r.text
    assert c.get(f"/servicefaelle/{fall['id']}", headers=h).json()["servicefall"]["status"] == "qualifiziert"
    with db.sitzungen() as s:
        assert s.get(Termin, t["termin"]["id"]).status == "abgesagt"
        k = s.scalars(select(Kommunikation).where(Kommunikation.kanal == "portal")).all()
        assert len(k) == 2 and "Bin verreist" in k[1].zusammenfassung
        assert s.scalar(select(Ereignis).where(Ereignis.typ == "termin.abgesagt")).payload["quelle"] == "portal"
    assert c.get(f"/portal/faelle/{fall['id']}?token={tok}").json()["termine"] == []
    # kurzfristige Absage (Termin in 2 h) nur telefonisch; vorgeschlagener Termin wird per Freigabe bestätigt
    fall2 = c.post("/servicefaelle", headers=h, json={"kunde_id": ids["kunde"], "anliegen": "Tropfender Hahn"}).json()
    t2 = c.post(f"/servicefaelle/{fall2['id']}/termine", headers=h, json={"ressource_id": ids["monteur_gas"], "beginn": (DEMO_ZEIT + timedelta(hours=2)).isoformat()}).json()
    tok2 = portal.token(fall2["id"])
    assert c.post(f"/portal/faelle/{fall2['id']}/termine/{t2['termin']['id']}?token={tok2}", json={"aktion": "absagen"}).status_code == 409
    assert c.post(f"/portal/faelle/{fall2['id']}/termine/{t2['termin']['id']}?token={tok}", json={"aktion": "bestaetigen"}).status_code == 404  # fremdes Token
    r = c.post(f"/portal/faelle/{fall2['id']}/termine/{t2['termin']['id']}?token={tok2}", json={"aktion": "bestaetigen"})
    assert r.status_code == 200 and "vorgemerkt" in r.json()["hinweis"]
    with db.sitzungen() as s:
        fr = s.scalars(select(Freigabe).where(Freigabe.aktion == "termin_bestaetigen", Freigabe.servicefall_id == fall2["id"])).all()
        assert any(f.begruendung == "Kunde hat im Portal bestätigt" for f in fr)


# --- DATEV ------------------------------------------------------------------------------------------------
def test_datev_anmeldung_rueckruf_und_uebermittlung(betrieb):  # noqa: F811
    c, db, ids, h = betrieb
    assert c.get("/integrationen/datev/anmeldung", headers=h).status_code == 409   # nicht konfiguriert
    c.put("/betrieb/stammdaten", headers=h, json={**STAMMDATEN, "basis_url": "https://shk.example.test"})
    r = c.put("/integrationen/kanzlei", headers=h, json={"kanaele": ["datev"], "datev": {"client_id": "cid", "client_secret": "geheim", "berater_nr": "12345", "mandant_nr": "67890"}})
    assert r.status_code == 200, r.text
    masken = c.get("/integrationen", headers=h).json()["ziele"]["kanzlei"]["datev"]
    assert masken["client_secret"] == "••••" and masken["client_id"] == "cid"
    assert c.put("/integrationen/finanz", headers=h, json={"kanaele": ["datev"], "datev": {"client_id": "cid"}}).status_code == 422   # secret fehlt
    a = c.get("/integrationen/datev/anmeldung", headers=h).json()
    url = urlparse(a["url"])
    q = parse_qs(url.query)
    assert url.netloc == "login.datev.de" and url.path == "/openidsandbox/authorize" and q["client_id"] == ["cid"]
    assert q["redirect_uri"] == ["https://shk.example.test/integrationen/datev/rueckruf"] and q["code_challenge_method"] == ["S256"]
    assert zustand_pruefen(q["state"][0]) == ids["mandant"] and zustand_pruefen("x.y") is None and zustand(ids["mandant"]) == q["state"][0]
    aufrufe = []

    def datev(request: httpx.Request) -> httpx.Response:
        aufrufe.append((request.url.host, request.url.path, request.headers.get("authorization", ""), request.content))
        if request.url.path.endswith("/token"):
            daten = parse_qs(request.content.decode())
            if daten["grant_type"] == ["authorization_code"]:
                assert daten["code_verifier"] and daten["code"] == ["abc"]
                return httpx.Response(200, json={"access_token": "at1", "refresh_token": "rt1", "expires_in": 300})
            assert daten["refresh_token"] == ["rt1"]
            return httpx.Response(200, json={"access_token": "at2", "expires_in": 300})
        if request.url.path.endswith("/documents"):
            return httpx.Response(201, json={"id": "doc-" + str(len(aufrufe))})
        if request.url.path.endswith("/dxso-jobs"):
            return httpx.Response(202, json={"id": "job-1"})
        return httpx.Response(404)

    c.app.state.datev_transport = httpx.MockTransport(datev)
    assert c.get("/integrationen/datev/rueckruf?error=access_denied&state=x").status_code == 400
    r = c.get(f"/integrationen/datev/rueckruf?code=abc&state={q['state'][0]}")
    assert r.status_code == 200 and "hergestellt" in r.text
    with db.sitzungen() as s:
        m = s.get(Mandant, ids["mandant"])
        block = m.integrationen["kanzlei"]["datev"]
        assert block.get("refresh_token") and "pkce_verifier" not in block   # ohne INTEGRATIONS_KEY im Test Klartext, sonst verschlüsselt
        assert s.scalar(select(Ereignis).where(Ereignis.typ == "integration.datev_verbunden")) is not None
    assert c.get("/integrationen", headers=h).json()["ziele"]["kanzlei"]["datev"]["refresh_token"] == "••••"
    # Übermittlung: Rechnung (finanz) und Belegpaket (kanzlei) über denselben Adapter
    c.put("/integrationen/finanz", headers=h, json={"kanaele": ["datev"], "datev": {"client_id": "cid", "client_secret": "geheim", "berater_nr": "12345", "mandant_nr": "67890", "refresh_token": "rt1"}})
    rechnung_stellen(c, h, ids, betrag_netto=100)
    heute = datetime.now(timezone.utc).strftime("%Y-%m")   # Rechnungen datieren mit der Systemuhr (Freigabewirkung)
    f = c.post("/freigaben", headers=h, json={"aktion": "belegversand_kanzlei", "payload": {"zeitraum": heute}}).json()
    c.post(f"/freigaben/{f['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "inhaber"})
    with db.sitzungen() as s:
        reg = Register(zusatz={"transport": httpx.MockTransport(datev)})
        z = abarbeiten(s, reg)
        eintraege = {e.aktion: e for e in s.scalars(select(OutboxEintrag).where(OutboxEintrag.aktion.in_(("rechnung_uebermitteln", "belegpaket"))))}
    assert z["wiederholung"] == 0 and z["tot"] == 0, z
    assert eintraege["rechnung_uebermitteln"].status == "zugestellt" and eintraege["belegpaket"].status == "zugestellt"
    pfade = [p for _, p, _, _ in aufrufe]
    assert "/platform-sandbox/accounting/v1/clients/12345-67890/documents" in pfade and "/platform-sandbox/accounting/v2/clients/12345-67890/dxso-jobs" in pfade
    assert all(a == "Bearer at2" for _, p, a, _ in aufrufe if "/accounting/" in p)
    doc = next(inhalt for _, p, _, inhalt in aufrufe if p.endswith("/documents"))
    assert b"Rechnungsausgang" in doc and b"%PDF" in doc
    stapel = next(inhalt for _, p, _, inhalt in aufrufe if p.endswith("/dxso-jobs"))
    assert b'"EXTF";' in stapel and b"EXTF_Buchungsstapel_" in stapel
    # zweiter Lauf: Rechnung ist referenziert, kein Doppelupload
    with db.sitzungen() as s:
        reg = Register(zusatz={"transport": httpx.MockTransport(datev)})
        abarbeiten(s, reg)
    assert len([p for _, p, _, _ in aufrufe if p.endswith("/documents")]) == 1
    assert "datev" in c.get("/integrationen", headers=h).json()["ziele"]["finanz"]["erlaubt"]
    try:
        DatevAdapter({"client_id": "x", "client_secret": "y"})
        raise AssertionError("berater_nr fehlt sollte abgewiesen werden")
    except Exception as e:  # noqa: BLE001
        assert "berater_nr" in str(e)


def test_belegpaket_enthaelt_debitorenliste(betrieb):  # noqa: F811
    c, db, ids, h = betrieb
    rechnung_stellen(c, h, ids, betrag_netto=50)
    f = c.post("/freigaben", headers=h, json={"aktion": "belegversand_kanzlei", "payload": {"zeitraum": DEMO_ZEIT.strftime("%Y-%m")}}).json()
    c.post(f"/freigaben/{f['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "inhaber"})
    with db.sitzungen() as s:
        from core.adapter.belegpaket import paket_erzeugen
        name, inhalt, _ = paket_erzeugen(s, s.get(Mandant, ids["mandant"]), {"zeitraum": datetime.now(timezone.utc).strftime("%Y-%m")})
    with zipfile.ZipFile(io.BytesIO(inhalt)) as z:
        deb = z.read("debitoren.csv").decode("utf-8-sig")
    zeilen = deb.splitlines()
    assert zeilen[0].startswith("Konto (Vorschlag);Unternehmensname;Name;Vorname") and zeilen[1].startswith("10000;;Beispiel;Familie;Weg 2;50667;Köln;DE;kunde@example.test")


def test_grosshandel_hilfsfunktionen():
    assert grosshandel.ids_warenkorb_xml([{"nummer": "A&B", "menge": 1.5}], "k1").count("<POSITION>") == 1
    assert "A&amp;B" in grosshandel.ids_warenkorb_xml([{"nummer": "A&B"}], "k1") and "<MENGE>1,5</MENGE>" in grosshandel.ids_warenkorb_xml([{"nummer": "x", "menge": 1.5}], "k1")
    assert json.loads(json.dumps(grosshandel.datanorm_lesen(DATANORM4), default=str))[2]["kennzeichen"] == "L"
