"""Belegeingang: XRechnung (UBL aus dem eigenen Export), ZUGFeRD (PDF mit eingebettetem XML), Text-Heuristik,
manuelle Erfassung, Prüfung, Übergabe im Belegpaket und Löschfristen."""
import io
import zipfile
from datetime import timedelta

import pytest
from pypdf import PdfReader, PdfWriter
from sqlalchemy import select

from core import belege
from core.adapter.belegpaket import paket_erzeugen
from core.demo import DEMO_ZEIT
from core.loeschung import loeschlauf
from core.modelle import Anhang, Beleg, Mandant
from tests.test_finanz import STAMMDATEN, rechnung_stellen


@pytest.fixture
def betrieb(umgebung):
    c, db, ids, h = umgebung
    assert c.put("/betrieb/stammdaten", headers=h, json=STAMMDATEN).status_code == 200
    return c, db, ids, h


def eigene_xrechnung(c, h, ids) -> bytes:
    r = rechnung_stellen(c, h, ids, positionen=[{"bezeichnung": "Umwälzpumpe", "menge": 1, "einheit": "Stk", "einzelpreis_netto": "150.00"}])
    return c.get(f"/rechnungen/{r['id']}/xrechnung", headers=h).content


def test_xrechnung_wird_strukturiert_gelesen(betrieb):
    c, db, ids, h = betrieb
    xml = eigene_xrechnung(c, h, ids)
    e = belege.erkennen(xml, "rechnung.xml", "application/xml")
    assert e["quelle"] == "xrechnung" and e["konfidenz"] == 1.0
    assert e["lieferant"] == "Max Muster" or "Demo" in e["lieferant"]
    assert str(e["brutto"]) == "178.50" and str(e["netto"]) == "150.00" and e["ust_satz"] == "19"
    assert e["nummer"].startswith("RE-") and e["positionen"][0]["bezeichnung"] == "Umwälzpumpe"
    # über die API: Upload legt Beleg und Anhang an
    antwort = c.post("/belege", headers=h, files={"datei": ("rechnung.xml", xml, "application/xml")}, data={"kategorie": "material"})
    assert antwort.status_code == 201, antwort.text
    b = antwort.json()
    assert b["status"] == "erfasst" and b["brutto"] == "178.50" and b["anhang_id"]
    assert c.get(f"/anhaenge/{b['anhang_id']}/inhalt", headers=h).content == xml


def test_zugferd_pdf_mit_eingebettetem_xml(betrieb):
    c, db, ids, h = betrieb
    xml = eigene_xrechnung(c, h, ids)
    w = PdfWriter()
    w.add_blank_page(width=595, height=842)
    w.add_attachment("factur-x.xml", xml)
    puffer = io.BytesIO()
    w.write(puffer)
    pdf = puffer.getvalue()
    assert PdfReader(io.BytesIO(pdf)).attachments
    e = belege.erkennen(pdf, "rechnung.pdf", "application/pdf")
    assert e["quelle"] == "zugferd" and str(e["brutto"]) == "178.50"


def test_text_heuristik_mit_konfidenz():
    text = """Sanitärgroßhandel Rheinland GmbH
Musterstraße 1, 50667 Köln
Rechnung Nr. 2026-4711          Rechnungsdatum: 03.10.2026
Kommission: Achternbusch Lindenallee
Nettobetrag        186,50 EUR
zzgl. 19 % MwSt.    35,44 EUR
Rechnungsbetrag    221,94 EUR
zahlbar bis 17.10.2026, 2 % Skonto bei Zahlung innerhalb 7 Tagen"""
    e = belege.aus_text(text)
    assert e["lieferant"].startswith("Sanitärgroßhandel Rheinland")
    assert e["nummer"] == "2026-4711" and str(e["datum"]) == "2026-10-03"
    assert str(e["brutto"]) == "221.94" and str(e["netto"]) == "186.50" and e["ust_satz"] == "19"
    assert str(e["faellig"]) == "2026-10-17" and str(e["skonto_prozent"]) in ("2", "2.0", "2.00")
    assert e["kommission"].startswith("Achternbusch") and e["konfidenz"] >= 0.75
    duenn = belege.aus_text("irgendein Zettel ohne Zahlen")
    assert duenn["konfidenz"] < 0.5 and duenn["hinweise"]


def test_foto_ohne_ocr_liefert_hinweis_und_manuelle_felder_gewinnen(betrieb, monkeypatch):
    c, db, ids, h = betrieb
    monkeypatch.setattr(belege, "ocr_text", lambda inhalt, endung: "")
    from PIL import Image
    bild = io.BytesIO()
    Image.new("RGB", (2400, 1800), "white").save(bild, "JPEG")
    antwort = c.post("/belege", headers=h, files={"datei": ("beleg.jpg", bild.getvalue(), "image/jpeg")},
                     data={"lieferant": "Tankstelle", "brutto": "84,20", "kategorie": "fahrzeug"})
    assert antwort.status_code == 201, antwort.text
    b = antwort.json()
    assert b["lieferant"] == "Tankstelle" and b["brutto"] == "84.20" and b["netto"] == "70.76" and b["umsatzsteuer"] == "13.44"
    assert b["erkennung"]["konfidenz"] == 0 and any("Texterkennung" in x for x in b["erkennung"]["hinweise"])
    with db.sitzungen() as s:
        a = s.get(Anhang, b["anhang_id"])
        assert a.groesse < 400_000 and Image.open(io.BytesIO(a.inhalt)).size[0] <= 1600  # verkleinert abgelegt


def test_manuell_pruefen_zuordnen_und_schutz_nach_uebergabe(betrieb):
    c, db, ids, h = betrieb
    assert c.post("/belege", headers=h, data={"kategorie": "material"}).status_code == 422
    fall = c.post("/voice/servicefall", headers=h, json={"telefon": "0221 1234567", "name": "Familie Beispiel", "anliegen": "Pumpe"}).json()
    b = c.post("/belege", headers=h, data={"lieferant": "Großhandel", "nummer": "4711", "netto": "100", "kategorie": "material"}).json()
    assert b["brutto"] == "119.00" and b["quelle"] == "manuell"
    assert c.get("/belege?status=erfasst", headers=h).json()[0]["id"] == b["id"]
    g = c.patch(f"/belege/{b['id']}", headers=h, json={"status": "geprueft", "servicefall_id": fall["servicefall_id"], "brutto": "120,00"}).json()
    assert g["status"] == "geprueft" and g["geprueft_von"] and g["brutto"] == "120.00"
    with db.sitzungen() as s:
        s.get(Beleg, b["id"]).status = "uebergeben"
        s.commit()
    assert c.patch(f"/belege/{b['id']}", headers=h, json={"netto": "1"}).status_code == 409
    assert c.delete(f"/belege/{b['id']}", headers=h).status_code == 409
    assert c.patch(f"/belege/{b['id']}", headers=h, json={"status": "bezahlt"}).status_code == 200


def test_belegpaket_enthaelt_eingangsbelege_und_er_buchungen(betrieb):
    c, db, ids, h = betrieb
    r = rechnung_stellen(c, h, ids, betrag_netto=100)
    zeitraum = r["datum"][:7]
    datum = r["datum"]
    gep = c.post("/belege", headers=h, data={"lieferant": "Großhandel West", "nummer": "LS-1", "netto": "200", "datum": datum, "kategorie": "material"}).json()
    c.patch(f"/belege/{gep['id']}", headers=h, json={"status": "geprueft"})
    c.post("/belege", headers=h, data={"lieferant": "Ungeprüft", "brutto": "10", "datum": datum})
    with db.sitzungen() as s:
        m = s.get(Mandant, ids["mandant"])
        name, inhalt, text = paket_erzeugen(s, m, {"zeitraum": zeitraum}, DEMO_ZEIT.date())
        assert "1 Eingangsbeleg" in text
        with zipfile.ZipFile(io.BytesIO(inhalt)) as z:
            namen = z.namelist()
            assert "eingangsbelege.csv" in namen and not any(n.startswith("eingangsbelege/") for n in namen)  # ohne Datei nur die Liste
            stapel = z.read(f"EXTF_Buchungsstapel_{zeitraum}.csv").decode("cp1252").splitlines()
        er = [zeile.split(";") for zeile in stapel[2:] if "ER " in zeile]
        assert len(er) == 1 and er[0][0] == '"238,00"' and er[0][6] == '"3400"' and er[0][7] == '"1600"'
        assert s.get(Beleg, gep["id"]).status == "uebergeben"
        assert s.scalar(select(Beleg).where(Beleg.lieferant == "Ungeprüft")).status == "erfasst"


def test_loeschlauf_belege_zehn_jahre_zeiten_zwei_jahre(betrieb):
    c, db, ids, h = betrieb
    b = c.post("/belege", headers=h, data={"lieferant": "Alt", "brutto": "10", "datum": "2015-01-05"}).json()
    c.post("/zeiten", headers=h, json={"art": "arbeit", "ressource_id": ids["monteur_gas"], "beginn": (DEMO_ZEIT - timedelta(days=800)).isoformat(),
                                        "ende": (DEMO_ZEIT - timedelta(days=800) + timedelta(hours=1)).isoformat()})
    with db.sitzungen() as s:
        m = s.get(Mandant, ids["mandant"])
        trocken = loeschlauf(s, m, DEMO_ZEIT, trocken=True)
        assert trocken["belege_geloescht"] == 1 and trocken["zeiten_geloescht"] == 1
        assert s.get(Beleg, b["id"]) is not None
        loeschlauf(s, m, DEMO_ZEIT)
        assert s.get(Beleg, b["id"]) is None
