"""Rechnungen, XRechnung, PDF, Adapter (E-Mail, SMS, Webhook, Lexware Office, sevDesk, Belegpaket) und
das Adapter-Register. Externe APIs sind durch httpx.MockTransport ersetzt; geprüft wird, was der Core
sendet, nicht das Verhalten der Anbieter."""
import hashlib
import hmac
import io
import json
import zipfile

import httpx
import pytest
from lxml import etree
from sqlalchemy import select

from core import domaene
from core.adapter import Register
from core.adapter.belegpaket import EXTF_SPALTEN, paket_erzeugen
from core.adapter.email import EmailAdapter
from core.adapter.lexware import LexwareAdapter
from core.adapter.sevdesk import SevdeskAdapter
from core.adapter.sms import SmsAdapter
from core.adapter.webhook import WebhookAdapter
from core.demo import DEMO_ZEIT
from core.geheimnisse import schluessel_erzeugen
from core.modelle import ExternalRef, Kunde, Mandant, OutboxEintrag, Rechnung
from core.rechnung import pdf, xrechnung
from core.zustellung import abarbeiten

STAMMDATEN = {"strasse": "Werkstr. 1", "plz": "50667", "ort": "Köln", "telefon": "0221 000", "email": "info@example.test",
              "inhaber": "Max Muster", "ust_id": "DE123456789", "iban": "DE02120300000000202051", "bic": "BYLADEM1001",
              "bank": "Musterbank", "zahlungsziel_tage": 14, "datev_beraternummer": "12345", "datev_mandantennummer": "67890"}
UBL = {"ubl": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
       "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
       "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"}


@pytest.fixture
def betrieb(umgebung):
    c, db, ids, h = umgebung
    assert c.put("/betrieb/stammdaten", headers=h, json=STAMMDATEN).status_code == 200
    c.patch(f"/kunden/{ids['kunde']}", headers=h, json={"email": "kunde@example.test", "strasse": "Weg 2", "plz": "50667", "ort": "Köln"})
    return c, db, ids, h


def rechnung_stellen(c, h, ids, **payload):
    f = c.post("/freigaben", headers=h, json={"aktion": "rechnung_erstellen", "servicefall_id": None,
                                               "payload": {"kunde_id": ids["kunde"], **payload}}).json()
    assert c.post(f"/freigaben/{f['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "inhaber"}).status_code == 200
    return c.get("/rechnungen", headers=h).json()[0]


def test_rechnung_mit_fortlaufender_nummer_und_summen(betrieb):
    c, db, ids, h = betrieb
    r = rechnung_stellen(c, h, ids, positionen=[{"bezeichnung": "Wartung Gastherme", "menge": 1, "einheit": "psch", "einzelpreis_netto": "189.00"},
                                                {"bezeichnung": "Anfahrt", "menge": 12, "einheit": "km", "einzelpreis_netto": "0.95"}])
    assert r["nummer"].startswith("RE-") and r["nummer"].endswith("-00001")
    assert r["netto"] == "200.40" and r["umsatzsteuer"] == "38.08" and r["brutto"] == "238.48"
    r2 = rechnung_stellen(c, h, ids, betrag_netto=50)
    assert r2["nummer"].endswith("-00002") and r2["brutto"] == "59.50"
    # Outbox: Buchhaltung (finanz) und Zustellung an den Kunden (endkunde) in derselben Transaktion
    with db.sitzungen() as s:
        ziele = {(e.ziel, e.aktion) for e in s.scalars(select(OutboxEintrag))}
    assert {("finanz", "rechnung_uebermitteln"), ("endkunde", "rechnung_zustellen")} <= ziele


def test_kleinunternehmer_ohne_umsatzsteuer(betrieb):
    c, db, ids, h = betrieb
    c.put("/betrieb/stammdaten", headers=h, json={**STAMMDATEN, "kleinunternehmer": True})
    r = rechnung_stellen(c, h, ids, betrag_netto=100)
    assert r["umsatzsteuer"] == "0.00" and r["brutto"] == "100.00"
    assert b"19 UStG" in c.get(f"/rechnungen/{r['id']}/xrechnung", headers=h).content


def test_pdf_und_xrechnung(betrieb):
    c, db, ids, h = betrieb
    r = rechnung_stellen(c, h, ids, betrag_netto=100, leitweg_id="991-12345-67")
    antwort = c.get(f"/rechnungen/{r['id']}/pdf", headers=h)
    assert antwort.status_code == 200 and antwort.content.startswith(b"%PDF")
    xml = c.get(f"/rechnungen/{r['id']}/xrechnung", headers=h).content
    baum = etree.fromstring(xml)
    assert baum.findtext("cbc:CustomizationID", namespaces=UBL).endswith("xrechnung_3.0")
    assert baum.findtext("cbc:ID", namespaces=UBL) == r["nummer"]
    assert baum.findtext("cbc:BuyerReference", namespaces=UBL) == "991-12345-67"
    assert baum.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=UBL) == "119.00"
    assert baum.findtext("cac:TaxTotal/cbc:TaxAmount", namespaces=UBL) == "19.00"
    assert baum.findtext("cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID", namespaces=UBL) == "DE123456789"
    assert len(baum.findall("cac:InvoiceLine", namespaces=UBL)) == 1


def test_storno_nur_mit_freigabe_klasse_c(betrieb):
    c, db, ids, h = betrieb
    r = rechnung_stellen(c, h, ids, betrag_netto=100)
    f = c.post(f"/rechnungen/{r['id']}/storno", headers=h, json={"grund": "Doppelt berechnet"}).json()
    assert f["risikoklasse"] == "C" and f["status"] == "offen"
    c.post(f"/freigaben/{f['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "inhaber"})
    liste = c.get("/rechnungen", headers=h).json()
    storno = next(x for x in liste if x["art"] == "storno")
    assert storno["storno_von_id"] == r["id"] and storno["brutto"] == "-119.00" and storno["nummer"].endswith("-00002")
    assert c.get(f"/rechnungen/{r['id']}", headers=h).json()["status"] == "storniert"
    assert c.post(f"/rechnungen/{r['id']}/storno", headers=h, json={"grund": "nochmal"}).status_code == 409
    with db.sitzungen() as s:
        m = s.get(Mandant, ids["mandant"])
        st = s.scalar(select(Rechnung).where(Rechnung.art == "storno"))
        assert etree.fromstring(xrechnung(st, m)).findtext("cbc:InvoiceTypeCode", namespaces=UBL) == "381"
        assert pdf(st, m).startswith(b"%PDF")


def test_email_adapter_terminbestaetigung_und_rechnung(betrieb):
    c, db, ids, h = betrieb
    rechnung_stellen(c, h, ids, betrag_netto=100)
    fall = c.post("/servicefaelle", headers=h, json={"kunde_id": ids["kunde"], "anliegen": "Heizung aus"}).json()
    t = c.post(f"/servicefaelle/{fall['id']}/termine", headers=h,
               json={"ressource_id": ids["monteur_gas"], "beginn": DEMO_ZEIT.replace(hour=10).isoformat()}).json()
    c.post(f"/freigaben/{t['freigabe']['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "buero"})
    gesendet = []
    adapter = EmailAdapter({"absender": "buero@example.test", "host": "x"}, versand=gesendet.append)
    with db.sitzungen() as s:
        ergebnis = abarbeiten(s, {"endkunde": adapter}, DEMO_ZEIT)
    assert ergebnis["zugestellt"] == 2
    betreffe = sorted(n["Subject"] for n in gesendet)
    assert betreffe[0].startswith("Rechnung RE-") and betreffe[1].startswith("Terminbestätigung 17.11.2026 10:00")
    rechnung = next(n for n in gesendet if n["Subject"].startswith("Rechnung"))
    anhaenge = [a.get_filename() for a in rechnung.iter_attachments()]
    assert anhaenge == [f"{rechnung['Subject'].split()[1]}.pdf"]
    assert "Monteur A" in next(n for n in gesendet if "Termin" in n["Subject"]).get_content()


def test_endkunde_ohne_kanal_wird_bueroaufgabe(betrieb):
    c, db, ids, h = betrieb
    c.patch(f"/kunden/{ids['kunde']}", headers=h, json={"email": ""})
    rechnung_stellen(c, h, ids, betrag_netto=10)
    with db.sitzungen() as s:
        s.get(Kunde, ids["kunde"]).email = None
        s.commit()
        ergebnis = abarbeiten(s, {"endkunde": EmailAdapter({"absender": "a@b", "host": "x"}, versand=lambda n: None)}, DEMO_ZEIT)
    assert ergebnis["ohne_kanal"] == 1
    assert c.get("/outbox?status=ohne_kanal", headers=h).json()[0]["aktion"] == "rechnung_zustellen"
    assert any(e["typ"] == "outbox.ohne_kanal" for e in c.get("/ereignisse", headers=h).json())


def test_sms_nur_mit_einwilligung(betrieb):
    c, db, ids, h = betrieb
    anfragen = []

    def antwort(r: httpx.Request) -> httpx.Response:
        anfragen.append(r)
        assert r.headers["X-Api-Key"] == "geheim"
        return httpx.Response(200, json={"success": "100", "messages": [{"id": "sms-1"}]})

    adapter = SmsAdapter({"api_key": "geheim", "absender": "Haustechnik"}, transport=httpx.MockTransport(antwort))
    c.post("/freigaben", headers=h, json={"aktion": "nachricht_an_endkunde", "payload": {"kunde_id": ids["kunde"], "text": "Wir kommen 10 Uhr"}})
    f = c.get("/freigaben", headers=h).json()[0]
    c.post(f"/freigaben/{f['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "buero"})
    with db.sitzungen() as s:
        assert abarbeiten(s, {"endkunde": adapter}, DEMO_ZEIT)["ohne_kanal"] == 1  # keine Einwilligung
        k = s.get(Kunde, ids["kunde"])
        k.einwilligungen = {"sms": "2026-11-01T10:00:00Z"}
        e = s.scalar(select(OutboxEintrag).where(OutboxEintrag.aktion == "nachricht"))
        e.status = "ausstehend"
        s.commit()
        assert abarbeiten(s, {"endkunde": adapter}, DEMO_ZEIT)["zugestellt"] == 1
    assert len(anfragen) == 1 and b"to=%2B492211234567" in anfragen[0].content and b"Wir+kommen" in anfragen[0].content


def test_webhook_signiert_und_liefert_kontext(betrieb):
    c, db, ids, h = betrieb
    fall = c.post("/servicefaelle", headers=h, json={"kunde_id": ids["kunde"], "anliegen": "Heizung aus"}).json()
    t = c.post(f"/servicefaelle/{fall['id']}/termine", headers=h,
               json={"ressource_id": ids["monteur_gas"], "beginn": DEMO_ZEIT.replace(hour=10).isoformat()}).json()
    c.post(f"/freigaben/{t['freigabe']['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "buero"})
    empfangen = []

    def antwort(r: httpx.Request) -> httpx.Response:
        erwartet = "sha256=" + hmac.new(b"s3cret", r.content, hashlib.sha256).hexdigest()
        assert r.headers["X-SHK-Signatur"] == erwartet and r.headers["X-SHK-Idempotenz"]
        empfangen.append(json.loads(r.content))
        return httpx.Response(200, json={"id": "auftrag-77"})

    adapter = WebhookAdapter({"url": "https://n8n.example.test/webhook/shk", "signatur_schluessel": "s3cret"},
                             transport=httpx.MockTransport(antwort))
    with db.sitzungen() as s:
        assert abarbeiten(s, {"fachsoftware": adapter}, DEMO_ZEIT)["zugestellt"] == 1
        ref = s.scalar(select(ExternalRef).where(ExternalRef.entitaet == "termin"))
        assert ref.externe_id == "auftrag-77" and ref.zielsystem == "webhook"
    assert empfangen[0]["aktion"] == "termin_upsert" and empfangen[0]["kunde"]["name"] == "Familie Beispiel"
    assert empfangen[0]["termin"]["monteur"] == "Monteur A"


def test_lexware_kontakt_und_rechnung_idempotent(betrieb):
    c, db, ids, h = betrieb
    rechnung_stellen(c, h, ids, betrag_netto=100)
    rechnung_stellen(c, h, ids, betrag_netto=20)
    aufrufe = []

    def antwort(r: httpx.Request) -> httpx.Response:
        aufrufe.append((r.method, r.url.path, r.url.params.get("finalize")))
        assert r.headers["Authorization"] == "Bearer lx-key"
        if r.url.path == "/v1/contacts" and r.method == "GET":
            return httpx.Response(200, json={"content": []})
        if r.url.path == "/v1/contacts":
            body = json.loads(r.content)
            assert body["person"] == {"firstName": "Familie", "lastName": "Beispiel"} and body["roles"] == {"customer": {}}
            return httpx.Response(201, json={"id": "kontakt-1"})
        if r.url.path == "/v1/invoices":
            body = json.loads(r.content)
            assert body["address"] == {"contactId": "kontakt-1"} and body["taxConditions"]["taxType"] == "net"
            assert body["lineItems"][0]["unitPrice"]["taxRatePercentage"] == 19.0
            return httpx.Response(201, json={"id": f"inv-{len(aufrufe)}"})
        return httpx.Response(404)

    adapter = LexwareAdapter({"api_key": "lx-key"}, transport=httpx.MockTransport(antwort))
    with db.sitzungen() as s:
        assert abarbeiten(s, {"finanz": adapter}, DEMO_ZEIT)["zugestellt"] == 2
        refs = s.scalars(select(ExternalRef).where(ExternalRef.zielsystem == "lexware")).all()
    assert [a[1] for a in aufrufe] == ["/v1/contacts", "/v1/contacts", "/v1/invoices", "/v1/invoices"]  # Kontakt nur einmal
    assert aufrufe[2][2] is None  # als Entwurf, nicht finalisiert
    assert {r.entitaet for r in refs} == {"kunde", "rechnung"} and len(refs) == 3


def test_sevdesk_rechnung(betrieb):
    c, db, ids, h = betrieb
    rechnung_stellen(c, h, ids, betrag_netto=100)

    def antwort(r: httpx.Request) -> httpx.Response:
        assert r.headers["Authorization"] == "sd-token"
        if r.url.path.endswith("/Contact") and r.method == "GET":
            return httpx.Response(200, json={"objects": [{"id": 4711}]})
        if r.url.path.endswith("/Invoice/Factory/saveInvoice"):
            body = json.loads(r.content)
            assert body["invoice"]["contact"] == {"id": "4711", "objectName": "Contact"} and body["invoice"]["status"] == "100"
            assert body["invoicePosSave"][0]["price"] == 100.0
            return httpx.Response(201, json={"objects": {"invoice": {"id": 9001}}})
        return httpx.Response(404)

    adapter = SevdeskAdapter({"api_token": "sd-token"}, transport=httpx.MockTransport(antwort))
    with db.sitzungen() as s:
        assert abarbeiten(s, {"finanz": adapter}, DEMO_ZEIT)["zugestellt"] == 1
        assert s.scalar(select(ExternalRef).where(ExternalRef.entitaet == "rechnung")).externe_id == "9001"


def test_belegpaket_mit_datev_stapel(betrieb):
    c, db, ids, h = betrieb
    r = rechnung_stellen(c, h, ids, betrag_netto=100)
    with db.sitzungen() as s:
        m = s.get(Mandant, ids["mandant"])
        zeitraum = r["datum"][:7]
        name, inhalt, text = paket_erzeugen(s, m, {"zeitraum": zeitraum}, DEMO_ZEIT.date())
    assert name == f"belege_{zeitraum}.zip" and "1 Ausgangsbelegen" in text
    with zipfile.ZipFile(io.BytesIO(inhalt)) as z:
        namen = z.namelist()
        assert f"rechnungen/{r['nummer']}.pdf" in namen and f"rechnungen/{r['nummer']}.xml" in namen
        stapel = z.read(f"EXTF_Buchungsstapel_{zeitraum}.csv").decode("cp1252").splitlines()
    assert stapel[0].startswith('"EXTF";700;21;"Buchungsstapel";13;') and '"12345";"67890"' in stapel[0]
    assert stapel[1].split(";")[0] == '"Umsatz (ohne Soll/Haben-Kz)"' and len(stapel[1].split(";")) == len(EXTF_SPALTEN)
    buchung = stapel[2].split(";")
    assert buchung[0] == '"119,00"' and buchung[1] == '"S"' and buchung[6] == '"1400"' and buchung[7] == '"8400"'
    assert buchung[EXTF_SPALTEN.index("Festschreibung")] == '"0"'


def test_belegpaket_ueber_freigabe_und_register(betrieb, tmp_path, monkeypatch):
    c, db, ids, h = betrieb
    monkeypatch.setenv("INTEGRATIONS_KEY", schluessel_erzeugen())
    r = rechnung_stellen(c, h, ids, betrag_netto=100)
    # Anbindung über die API: Geheimnis wird verschlüsselt gespeichert und maskiert ausgegeben
    antwort = c.put("/integrationen/finanz", headers=h, json={"kanaele": ["lexware"], "lexware": {"api_key": "lx-key"}})
    assert antwort.status_code == 200, antwort.text
    assert c.get("/integrationen", headers=h).json()["ziele"]["finanz"]["lexware"]["api_key"] == "••••"
    with db.sitzungen() as s:
        gespeichert = s.get(Mandant, ids["mandant"]).integrationen["finanz"]["lexware"]["api_key"]
    assert gespeichert.startswith("enc:") and "lx-key" not in gespeichert
    # maskierter Wert beim erneuten Speichern behält das Geheimnis
    c.put("/integrationen/finanz", headers=h, json={"kanaele": ["lexware"], "lexware": {"api_key": "••••", "als_entwurf": False}})
    with db.sitzungen() as s:
        assert s.get(Mandant, ids["mandant"]).integrationen["finanz"]["lexware"]["api_key"] == gespeichert
    assert c.put("/integrationen/finanz", headers=h, json={"kanaele": ["sms"]}).status_code == 422
    assert c.put("/integrationen/kanzlei", headers=h, json={"kanaele": ["belegpaket"], "belegpaket": {"verzeichnis": str(tmp_path)}}).status_code == 200
    f = c.post("/kanzlei/belegpaket", headers=h, json={"zeitraum": r["datum"][:7]}).json()
    assert f["risikoklasse"] == "C"
    c.post(f"/freigaben/{f['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "inhaber"})

    def lexware(r: httpx.Request) -> httpx.Response:
        assert r.headers["Authorization"] == "Bearer lx-key"  # entschlüsselt beim Bauen des Adapters
        return httpx.Response(200, json={"content": [{"id": "k"}]} if r.method == "GET" else {"id": "inv"})

    register = Register(tmp_path / "csv", {"transport": httpx.MockTransport(lexware)})
    with db.sitzungen() as s:
        ergebnis = abarbeiten(s, register, DEMO_ZEIT)
    assert ergebnis["zugestellt"] >= 2  # finanz über Lexware, kanzlei als Datei; endkunde per CSV-Rückfall
    assert list((tmp_path / ids["mandant"]).glob("belege_*.zip"))
    assert (tmp_path / "csv").exists()


def test_domaene_rechnung_ohne_kunde_schlaegt_fehl(betrieb):
    c, db, ids, h = betrieb
    f = c.post("/freigaben", headers=h, json={"aktion": "rechnung_erstellen", "payload": {"betrag_netto": 1}}).json()
    assert c.post(f"/freigaben/{f['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "inhaber"}).status_code == 422
    with db.sitzungen() as s:
        assert s.scalar(select(Rechnung)) is None and s.get(Mandant, ids["mandant"]).rechnungsnummer_zaehler == 0
    assert domaene.risikoklasse("storno") == "C"
