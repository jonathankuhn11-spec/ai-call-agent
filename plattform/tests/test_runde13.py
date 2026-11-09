"""Runde 13: Verkaufsreife gegen die Problemcodes der Leads (P1–P10).

Karriereseite mit JobPosting und Kurzbewerbung (P10), Anfrageformular mit Fotos und Notfallkennzeichnung (P2, P5),
Notfallalarm für schriftliche Kanäle, Rufumleitungsassistent für Mobilnummern (P7), Website-Check als Vertriebsinstrument
(P1, P6, P9), Editionen gemäß Preisliste sowie Regressionen (CO-Fehlalarm bei „GmbH & Co. KG“, Formularkennung)."""
from __future__ import annotations

import io
import json
import re
from datetime import timedelta

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

from core import loeschung, produkt, rufumleitung, webcheck
from core.demo import DEMO_ZEIT
from core.modelle import Anfrage, Anhang, Bewerbung, Mandant, OutboxEintrag, Servicefall
from core.regeln import dringlichkeit_bewerten

HEUTE = DEMO_ZEIT.date()
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def jpeg(breite: int = 40) -> bytes:
    puffer = io.BytesIO()
    Image.new("RGB", (breite, breite), (200, 40, 40)).save(puffer, "JPEG")
    return puffer.getvalue()


def website_an(c, h, **mehr):
    r = c.put("/betrieb/website", headers=h, json={"aktiv": True, "slug": "demo-haustechnik", **mehr})
    assert r.status_code == 200, r.text
    return r.json()


def hinweise(db, mandant_id):
    with db.sitzungen() as s:
        return [e.payload for e in s.scalars(select(OutboxEintrag).where(OutboxEintrag.mandant_id == mandant_id, OutboxEintrag.ziel == "inhaber",
                                                                        OutboxEintrag.aktion == "hinweis"))]


def ntfy_an(c, h):
    """Push-Hinweise aktivieren (core.hinweise.aktiv)."""
    r = c.put("/integrationen/inhaber", headers=h, json={"kanaele": ["ntfy"], "ntfy": {"basis_url": "https://ntfy.example.invalid", "topic": "kuhn-test"}})
    assert r.status_code == 200, r.text


STELLEN = [{"titel": "Anlagenmechaniker SHK (m/w/d)", "art": "vollzeit", "ort": "Köln", "gehalt": "ab 18 € je Stunde", "start": "sofort",
            "aufgaben": "Wartung und Störungsdienst\nBadmontage", "profil": ["Ausbildung als Anlagenmechaniker"], "wir_bieten": ["Firmenwagen"]},
           {"titel": "Bürokraft Teilzeit", "art": "teilzeit", "stunden": "20 Std./Woche"},
           {"titel": "Abgelaufen", "art": "minijob", "gueltig_bis": (HEUTE - timedelta(days=1)).isoformat()},
           {"titel": "Inaktiv", "art": "aushilfe", "aktiv": False}]


# --- P10 Karriereseite ------------------------------------------------------------------------------------
def test_karriereseite_jobposting_und_sitemap(umgebung):
    c, db, ids, h = umgebung
    st = website_an(c, h, stellen=STELLEN)
    stellen = st["konfig"]["stellen"]
    assert len(stellen) == 4 and all(re.match(r"^[a-z0-9-]{3,40}$", x["id"]) for x in stellen)
    assert stellen[0]["aufgaben"] == ["Wartung und Störungsdienst", "Badmontage"] and stellen[0]["veroeffentlicht"] == HEUTE.isoformat()
    # erneutes Speichern behält IDs und Veröffentlichungsdatum
    wieder = website_an(c, h, stellen=[{**x, "veroeffentlicht": "2020-01-01"} for x in stellen])["konfig"]["stellen"]
    assert [x["id"] for x in wieder] == [x["id"] for x in stellen] and wieder[0]["veroeffentlicht"] == HEUTE.isoformat()
    assert c.put("/betrieb/website", headers=h, json={"stellen": [{"titel": "X", "art": "beamter"}]}).status_code == 422

    seite = c.get("/web/demo-haustechnik/karriere")
    assert seite.status_code == 200 and "Anlagenmechaniker SHK" in seite.text and "Bürokraft Teilzeit" in seite.text
    assert "Abgelaufen" not in seite.text and "Inaktiv" not in seite.text and 'name="datenschutz"' in seite.text
    start = c.get("/web/demo-haustechnik").text
    assert "Wir stellen ein" in start and "/web/demo-haustechnik/karriere" in start

    sid = stellen[0]["id"]
    detail = c.get(f"/web/demo-haustechnik/karriere/{sid}")
    assert detail.status_code == 200 and "Firmenwagen" in detail.text and "ab 18 € je Stunde" in detail.text
    ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', detail.text).group(1))
    assert ld["@type"] == "JobPosting" and ld["employmentType"] == "FULL_TIME" and ld["datePosted"] == HEUTE.isoformat()
    assert ld["hiringOrganization"]["name"] and ld["jobLocation"]["address"]["addressLocality"] == "Köln" and "<li>Badmontage</li>" in ld["description"]
    assert f'/karriere/{sid}"' in detail.text  # kanonische Adresse der Stelle
    assert c.get(f"/web/demo-haustechnik/karriere/{stellen[2]['id']}").status_code == 404   # abgelaufen
    sm = c.get("/web/demo-haustechnik/sitemap.xml").text
    assert f"/karriere/{sid}</loc>" in sm and f"/karriere/{stellen[2]['id']}<" not in sm

    c.put("/betrieb/website", headers=h, json={"bereiche": {"karriere": False}})
    assert c.get("/web/demo-haustechnik/karriere").status_code == 404 and "Wir stellen ein" not in c.get("/web/demo-haustechnik").text


def test_kurzbewerbung_unterlagen_status_und_loeschfrist(umgebung):
    c, db, ids, h = umgebung
    ntfy_an(c, h)
    sid = website_an(c, h, stellen=STELLEN)["konfig"]["stellen"][0]["id"]
    basis = {"name": "Mia Monteurin", "email": "mia@example.invalid", "stelle_id": sid, "datenschutz": "1", "start": "01.03."}
    r = c.post("/web/demo-haustechnik/karriere/bewerbung", data=basis, files=[("unterlagen", ("lebenslauf.pdf", PDF, "application/pdf"))], follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/karriere?gesendet=1")
    # identische Doppelabsendung in derselben Stunde wird nicht doppelt gespeichert
    c.post("/web/demo-haustechnik/karriere/bewerbung", data=basis, follow_redirects=False)
    liste = c.get("/bewerbungen", headers=h).json()
    assert len(liste) == 1 and liste[0]["stelle_titel"].startswith("Anlagenmechaniker") and liste[0]["unterlagen"][0]["mime"] == "application/pdf"
    b = liste[0]
    pdf = c.get(f"/bewerbungen/{b['id']}/unterlagen/{b['unterlagen'][0]['id']}", headers=h)
    assert pdf.status_code == 200 and pdf.content == PDF and pdf.headers["content-disposition"].startswith("attachment")
    # Unterlagen sind nicht über den allgemeinen Anhang-Endpunkt (Monteurrecht) erreichbar
    assert c.get(f"/anhaenge/{b['unterlagen'][0]['id']}/inhalt", headers=h).status_code in (403, 404, 422)
    assert any(x.get("titel") == "Neue Bewerbung" for x in hinweise(db, ids["mandant"]))

    # Validierung: Datenschutz, Kontakt, Dateityp per Signatur, Initiativbewerbung, Honeypot
    def senden(daten, dateien=None):
        return c.post("/web/demo-haustechnik/karriere/bewerbung", data=daten, files=dateien, headers={"Accept": "application/json"})
    assert senden({**basis, "datenschutz": ""}).status_code == 422
    assert senden({"name": "Ohne Kontakt", "datenschutz": "1"}).status_code == 422
    assert senden({**basis, "name": "Exe"}, [("unterlagen", ("lebenslauf.pdf", b"MZ\x90\x00boese", "application/pdf"))]).status_code == 422
    assert senden({**basis, "name": "Alt", "stelle_id": "gibt-es-nicht"}).status_code == 422
    html_fehler = c.post("/web/demo-haustechnik/karriere/bewerbung", data={**basis, "datenschutz": ""})
    assert html_fehler.status_code == 422 and "Datenschutzhinweise" in html_fehler.text
    assert senden({"name": "Initiativ", "telefon": "0221 555", "datenschutz": "1", "talentpool": "1"}).status_code == 200
    assert senden({"name": "Bot", "email": "b@example.invalid", "datenschutz": "1", "website": "spam"}).status_code == 200
    alle = c.get("/bewerbungen", headers=h).json()
    assert len(alle) == 2 and {x["stelle_titel"] for x in alle} == {"Initiativbewerbung", b["stelle_titel"]}

    # Status und Löschfrist: Absage → 180 Tage, Talentpool → 730 Tage
    assert c.patch(f"/bewerbungen/{b['id']}", headers=h, json={"status": "absage", "notiz": "Gespräch geführt"}).json()["abgeschlossen"]
    initiativ = next(x for x in alle if x["stelle_titel"] == "Initiativbewerbung")
    c.patch(f"/bewerbungen/{initiativ['id']}", headers=h, json={"status": "absage"})
    assert c.patch(f"/bewerbungen/{b['id']}", headers=h, json={"status": "unbekannt"}).status_code == 422
    with db.sitzungen() as s:
        m = s.get(Mandant, ids["mandant"])
        assert loeschung.loeschlauf(s, m, DEMO_ZEIT + timedelta(days=170), trocken=True)["bewerbungen_geloescht"] == 0
        z = loeschung.loeschlauf(s, m, DEMO_ZEIT + timedelta(days=200))
        assert z["bewerbungen_geloescht"] == 1
        rest = s.scalars(select(Bewerbung).where(Bewerbung.mandant_id == m.id)).all()
        assert [x.talentpool for x in rest] == [True]
        assert not s.scalars(select(Anhang).where(Anhang.bezug_typ == "bewerbung", Anhang.bezug_id == b["id"])).all()
        assert loeschung.loeschlauf(s, m, DEMO_ZEIT + timedelta(days=740))["bewerbungen_geloescht"] == 1
    assert c.delete(f"/bewerbungen/{initiativ['id']}", headers=h).status_code == 404


def test_bewerbungen_nur_fuer_buero_und_inhaber():
    from fastapi.testclient import TestClient

    from core.api import app_erzeugen
    from core.auth import Authentifizierung, OidcPruefer
    from core.db import Datenbank
    from core.demo import stammdaten_anlegen
    from tests.test_auth import ISSUER, SCHLUESSEL, token
    auth = Authentifizierung(api_key="maschine", oidc=OidcPruefer(ISSUER, "shk-core", client_id="shk-buero", schluessel=lambda _t: SCHLUESSEL.public_key()))
    c = TestClient(app_erzeugen(Datenbank("sqlite://"), uhr=lambda: DEMO_ZEIT, auth=auth))
    c.headers["X-API-Key"] = "maschine"
    ids = stammdaten_anlegen(c)
    del c.headers["X-API-Key"]
    assert c.get("/bewerbungen", headers=token(ids["mandant"], ["monteur"], name="m.monteur")).status_code == 403
    assert c.get("/bewerbungen", headers=token(ids["mandant"], ["buero"])).status_code == 200
    assert c.get("/betrieb/rufumleitung", headers=token(ids["mandant"], ["buero"])).status_code == 403


# --- P2/P5 Anfrageformular mit Fotos und Notfall --------------------------------------------------------
def test_anfrage_mit_fotos_und_notfall_alarmiert(umgebung):
    c, db, ids, h = umgebung
    ntfy_an(c, h)
    website_an(c, h, notdienst_telefon="0221 999111")
    formular = c.get("/web/demo-haustechnik").text
    assert 'enctype="multipart/form-data"' in formular and 'name="fotos"' in formular and 'name="notfall"' in formular
    daten = {"name": "Erika Muster", "telefon": "0221 4455667", "nachricht": "Unter der Spüle tropft es, bitte schnell kommen", "notfall": "1"}
    r = c.post("/web/demo-haustechnik/anfrage", data=daten, files=[("fotos", ("spuele.jpg", jpeg(), "image/jpeg")), ("fotos", ("typenschild.jpg", jpeg(60), "image/jpeg"))],
               follow_redirects=False)
    assert r.status_code == 303 and "gesendet=2" in r.headers["location"]
    bestaetigung = c.get("/web/demo-haustechnik?gesendet=2")
    assert "Bereitschaftsdienst" in bestaetigung.text and "0221 999111" in bestaetigung.text and "no-store" in bestaetigung.headers["cache-control"]
    with db.sitzungen() as s:
        a = s.scalar(select(Anfrage).where(Anfrage.mandant_id == ids["mandant"], Anfrage.name == "Erika Muster"))
        fall = s.get(Servicefall, a.servicefall_id)
        assert fall.dringlichkeit == "notdienst"
        fotos = s.scalars(select(Anhang).where(Anhang.bezug_typ == "servicefall", Anhang.bezug_id == fall.id)).all()
        assert len(fotos) == 2 and {f.art for f in fotos} == {"foto"}
    alarme = [x for x in hinweise(db, ids["mandant"]) if x.get("art") == "notdienst"]
    assert alarme and "Rückruf" in alarme[0]["text"] and alarme[0]["prioritaet"] == 5

    # nur Fotos: Signaturprüfung, Obergrenze drei
    def senden(dateien):
        return c.post("/web/demo-haustechnik/anfrage", data={**daten, "notfall": "", "name": f"Test {len(dateien)}"}, files=dateien, headers={"Accept": "application/json"})
    assert senden([("fotos", ("rechnung.pdf", PDF, "image/jpeg"))]).status_code == 422
    assert senden([("fotos", (f"f{i}.jpg", jpeg(), "image/jpeg")) for i in range(4)]).status_code == 422
    ok = senden([("fotos", ("f.jpg", jpeg(), "image/jpeg"))])
    assert ok.status_code == 200 and ok.json() == {"status": "angenommen", "notdienst": False, "fotos": 1}


def test_schriftlicher_notfall_per_email_alarmiert(umgebung):
    from core import anfragen
    c, db, ids, h = umgebung
    ntfy_an(c, h)
    with db.sitzungen() as s:
        m = s.get(Mandant, ids["mandant"])
        anfragen.anfrage_verarbeiten(s, m, "email", "<rohr@example.invalid>", "Herr Nass", "0221 3344556", None, "Rohrbruch im Keller",
                                     "Wasser läuft aus der Wand", DEMO_ZEIT)
        anfragen.anfrage_verarbeiten(s, m, "email", "<gas@example.invalid>", "Frau Vorsicht", None, "f@example.invalid", "Gasgeruch",
                                     "Es riecht nach Gas im Flur", DEMO_ZEIT)
        s.commit()
    arten = [x["art"] for x in hinweise(db, ids["mandant"])]
    assert "notdienst" in arten and "sicherheit" in arten


def test_regression_co_kg_und_austausch_kein_alarm():
    signatur = "Bitte um Angebot: Heizung austauschen.\nMit freundlichen Grüßen\nMüller Haustechnik GmbH & Co. KG"
    assert dringlichkeit_bewerten(signatur, 1).dringlichkeit == "normal"
    assert dringlichkeit_bewerten("Schmidt GmbH & Co KG", 1).sicherheitshinweis is None
    assert dringlichkeit_bewerten("Der CO-Melder piept", 6).sicherheitshinweis
    assert dringlichkeit_bewerten("Heizung ausgefallen", 1).dringlichkeit == "hoch"


def test_webformular_kennung_deterministisch(umgebung):
    c, db, ids, h = umgebung
    token = c.put("/betrieb/webformular", headers=h, json={"aktiv": True}).json()["url"].rsplit("/", 1)[-1]
    daten = {"name": "Doppelt", "telefon": "0221 1112223", "nachricht": "Bitte Wartungstermin"}
    for _ in range(2):
        assert c.post(f"/anfragen/web/{token}", json=daten).status_code == 200
    with db.sitzungen() as s:
        kennungen = s.scalars(select(Anfrage.externe_id).where(Anfrage.mandant_id == ids["mandant"], Anfrage.name == "Doppelt")).all()
    assert len(kennungen) == 1 and re.match(r"^web:[0-9a-f]{24}$", kennungen[0])


# --- Editionen gemäß Preisliste -------------------------------------------------------------------------
def test_editionen_entsprechen_preisliste(umgebung):
    c, db, ids, h = umgebung
    e = produkt.EDITIONEN
    assert [(e[k]["titel"], e[k]["preis_monat"], e[k]["einrichtung"], e[k]["limits"]["sprachminuten_monat"]) for k in ("starter", "pro", "enterprise")] == \
        [("Basis", 349, 1490, 300), ("Pro", 649, 2990, 600), ("Premium", 999, 4990, 1000)]
    assert "voice" in e["starter"]["features"] and "website" in e["starter"]["features"]   # Basis = KI-Telefonagent „Erreichbar“
    assert produkt.mehrminuten(312.4, 300) == 13 and produkt.mehrminuten(10, 300) == 0 and produkt.mehrminuten(5, None) == 0
    assert produkt.produkt()["name"] == "KUHN" and produkt.produkt()["mehrminute_netto"] == "0.25"
    uebersicht = c.get("/betreiber/mandanten").json()
    zeile = next(x for x in uebersicht["mandanten"] if x["id"] == ids["mandant"])
    assert {"sprachminuten_monat", "mehrminuten", "mehrminuten_netto"} <= set(zeile) and "mehrminuten_netto" in uebersicht["summen"]


# --- P7 Rufumleitung --------------------------------------------------------------------------------------
def test_rufumleitung_codes_und_einrichtungsschritt(umgebung):
    c, db, ids, h = umgebung
    ohne = c.get("/betrieb/rufumleitung", headers=h).json()
    assert ohne["codes"] is None and "Zielrufnummer" in ohne["hinweise"][0]
    assert c.put("/betrieb/rufumleitung", headers=h, json={"ziel": "kein telefon"}).status_code == 422
    r = c.put("/betrieb/rufumleitung", headers=h, json={"ziel": "0221 9876543"}).json()
    assert r["ziel"] == "+492219876543" and r["codes"]["nichtannahme"]["aktivieren"]["code"] == "**61*+492219876543*11*20#"
    assert r["codes"]["alle_bedingten"]["aktivieren"]["tel"] == "tel:%2A%2A004%2A%2B492219876543%2A11%2A20%23"
    assert c.get("/betrieb/rufumleitung?sekunden=17", headers=h).json()["sekunden"] == 15     # Netz erlaubt nur 5er-Schritte
    assert rufumleitung.ist_mobil("0171 2345678") and not rufumleitung.ist_mobil("0221 9876543")
    schritte = {x["schluessel"]: x for x in c.get("/betrieb/einrichtung", headers=h).json()["schritte"]}
    assert schritte["rufumleitung"]["erledigt"] is False
    c.put("/betrieb/rufumleitung", headers=h, json={"eingerichtet": True})
    assert {x["schluessel"]: x for x in c.get("/betrieb/einrichtung", headers=h).json()["schritte"]}["rufumleitung"]["erledigt"] is True


# --- P1/P6/P9 Website-Check -------------------------------------------------------------------------------
ALTSEITE = """<html><head><meta name="generator" content="WordPress 5.2.4"><link href="https://fonts.googleapis.com/css?family=Open+Sans" rel="stylesheet">
<script src="https://www.googletagmanager.com/gtag/js?id=G-1"></script><script src="/wp-includes/js/jquery/jquery.js?ver=1.12.4"></script></head>
<body><h1>Sanitär Altmann</h1><p>24 Stunden Notdienst: <a href="tel:01701234567">0170 1234567</a></p><p>Lorem ipsum dolor sit amet. Wir sind Ihr Fachbetrieb für Heizung und Bad
in Hamm und Umgebung, seit 1985 familiengeführt, mit Meisterbrief und vielen zufriedenen Kunden aus der ganzen Region.</p>
<iframe src="https://www.google.com/maps/embed?pb=1"></iframe><footer>© 2019 Sanitär Altmann · <a href="/impressum">Impressum</a></footer></body></html>"""
GUTE_SEITE = """<html><head><meta name="viewport" content="width=device-width"><script src="/cookiebot.js"></script></head><body>
<nav><a href="/karriere">Karriere</a><a href="/kundenportal">Kundenportal</a></nav><h1>Haustechnik Neu</h1><p>Online-Termin buchen in zwei Minuten.
Wir sind Ihr Fachbetrieb für Heizung, Bad und Wärmepumpen in Münster. Rufen Sie uns an oder schreiben Sie uns über das Formular.</p>
<form><input type="email" name="email"><textarea name="nachricht"></textarea></form><p>Telefon <a href="tel:+492511234567">0251 1234567</a></p>
<footer>© {jahr} · <a href="/impressum">Impressum</a> · <a href="/datenschutz">Datenschutz</a></footer></body></html>"""


def seiten_transport(seiten: dict[str, tuple[int, str]]):
    def handler(req: httpx.Request) -> httpx.Response:
        schluessel = f"{req.url.scheme}://{req.url.host}{req.url.path}"
        if schluessel not in seiten:
            return httpx.Response(404, text="nicht gefunden")
        status, inhalt = seiten[schluessel]
        if 300 <= status < 400:
            return httpx.Response(status, headers={"location": inhalt})
        return httpx.Response(status, text=inhalt, headers={"content-type": "text/html; charset=utf-8"})
    return httpx.MockTransport(handler)


def test_website_check_altseite_belegt_probleme():
    t = seiten_transport({"http://altmann.example/": (200, ALTSEITE), "https://altmann.example/": (301, "http://altmann.example/"),
                          "http://altmann.example/impressum": (200, "<p>Angaben gemäß § 5 TMG: Max Mustermann</p>")})
    e = webcheck.pruefen("altmann.example", t, HEUTE)
    codes = {b["code"] for b in e["befunde"]}
    assert {"P2", "P3", "P5", "P6", "P7", "P9", "P10"} <= codes and "P1" not in codes
    titel = " | ".join(b["titel"] + " " + b["beleg"] for b in e["befunde"])
    for teil in ("HTTPS", "WordPress", "Google Fonts", "Tracking", "Google Maps", "Datenschutzerklärung", "Platzhalter", "TMG", "jQuery", "Mobilnummer", "Notdienst nur telefonisch"):
        assert teil in titel, teil
    assert e["cms"] == "WordPress 5.2.4" and e["bedarf_score"] >= 80 and e["befunde"][0]["schwere"] == "hoch"
    assert e["loesungen"]["P7"].startswith("T1") and "P9" in webcheck.bericht_text(e)


def test_website_check_gute_seite_und_fehlerfaelle():
    jahr = HEUTE.year
    t = seiten_transport({"https://neu.example/": (200, GUTE_SEITE.replace("{jahr}", str(jahr))), "https://neu.example/impressum": (200, "<p>§ 5 DDG</p>"),
                          "https://neu.example/datenschutz": (200, "<p>Datenschutz</p>")})
    e = webcheck.pruefen("https://neu.example", t, HEUTE)
    assert e["probleme"] == [] and e["bedarf_score"] == 0 and len(e["seiten"]) == 3
    weg = webcheck.pruefen("https://weg.example", seiten_transport({}), HEUTE)
    assert weg["probleme"] == ["P1"]
    with pytest.raises(webcheck.Fehler):
        webcheck.url_normalisieren("ftp://x.example")
    with pytest.raises(webcheck.Fehler, match="nicht öffentliche"):
        webcheck._oeffentlich("127.0.0.1")
    with pytest.raises(webcheck.Fehler, match="nicht öffentliche"):
        webcheck.pruefen("http://127.0.0.1:8100/metrics", None, HEUTE)


def test_website_check_api_nur_betreiber(umgebung):
    c, db, ids, h = umgebung
    c.app.state.webcheck_transport = seiten_transport({"https://altmann.example/": (200, ALTSEITE)})
    r = c.post("/betreiber/website-check", json={"url": "altmann.example"})
    assert r.status_code == 200 and "P9" in r.json()["probleme"] and r.json()["bericht"].startswith("Website-Check")
    assert c.post("/betreiber/website-check", json={"url": "ftp://x.example"}).status_code == 422
