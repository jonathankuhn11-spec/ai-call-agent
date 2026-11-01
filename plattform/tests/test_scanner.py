"""Scan-Eingang (Kamera-Scan, Scan-to-Mail, Eingangsordner) und Belegübergabe an Steuertools
(Lexware Office Files-API, sevDesk Voucher, DATEV-Belegtransfer-Ordner, Upload-Mail). Externe APIs sind
durch httpx.MockTransport ersetzt; geprüft wird, was der Core sendet."""
import io
import json
from email.message import EmailMessage

import httpx
import pytest
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import select

from core import scanner
from core.adapter import Register, konfiguration_pruefen
from core.adapter.belegtransfer import BelegtransferAdapter
from core.adapter.email import EmailAdapter
from core.adapter.lexware import LexwareAdapter
from core.adapter.sevdesk import SevdeskAdapter
from core.demo import DEMO_ZEIT
from core.modelle import Beleg, ExternalRef, Mandant, OutboxEintrag
from core.zustellung import abarbeiten
from tests.test_finanz import STAMMDATEN


@pytest.fixture
def betrieb(umgebung, monkeypatch):
    c, db, ids, h = umgebung
    assert c.put("/betrieb/stammdaten", headers=h, json=STAMMDATEN).status_code == 200
    from core import belege
    monkeypatch.setattr(belege, "ocr_text", lambda inhalt, endung: "")  # keine Texterkennung im Test
    return c, db, ids, h


def foto(farbe=(200, 200, 200), groesse=(640, 480)) -> bytes:
    puffer = io.BytesIO()
    Image.new("RGB", groesse, farbe).save(puffer, format="JPEG")
    return puffer.getvalue()


class Postfach:
    def __init__(self, nachrichten):
        self.nachrichten, self.gelesen = nachrichten, []

    def ungelesene(self):
        return self.nachrichten

    def als_gelesen(self, i):
        self.gelesen.append(i)


def scan_mail(absender="scanner@betrieb.test", message_id="<scan-1@betrieb.test>", anhaenge=(("scan.pdf", "application/pdf"),)) -> bytes:
    n = EmailMessage()
    n["From"], n["To"], n["Subject"], n["Message-ID"] = absender, "belege@betrieb.test", "Scan vom Gerät", message_id
    n.set_content("Automatisch gesendet vom Multifunktionsgerät.")
    for name, mime in anhaenge:
        haupt, unter = mime.split("/")
        inhalt = scanner.bilder_zu_pdf([foto()]) if unter == "pdf" else foto()
        n.add_attachment(inhalt, maintype=haupt, subtype=unter, filename=name)
    return bytes(n)


def test_bilder_werden_zu_mehrseitigem_pdf():
    pdf = scanner.bilder_zu_pdf([foto(), foto((50, 50, 50), (3000, 4000)), foto()])
    leser = PdfReader(io.BytesIO(pdf))
    assert len(leser.pages) == 3
    assert float(leser.pages[1].mediabox.width) < float(leser.pages[1].mediabox.height)  # Hochformat bleibt
    with pytest.raises(ValueError):
        scanner.bilder_zu_pdf([b"kein bild"])
    with pytest.raises(ValueError):
        scanner.bilder_zu_pdf([])


def test_kamera_scan_ueber_api_ist_dublettensicher(betrieb):
    c, db, ids, h = betrieb
    seiten = [("bilder", ("s1.jpg", foto(), "image/jpeg")), ("bilder", ("s2.jpg", foto((90, 90, 90)), "image/jpeg"))]
    antwort = c.post("/belege/scan", headers=h, files=seiten, data={"kategorie": "fremdleistung", "notiz": "Rechnung Elektriker"})
    assert antwort.status_code == 201, antwort.text
    b = antwort.json()
    assert b["status"] == "erfasst" and b["kategorie"] == "fremdleistung" and b["erkennung"]["eingang"] == "kamera"
    assert b["import_kennung"].startswith("kamera:sha256:") and b["uebergaben"] == []
    anhang = c.get(f"/anhaenge/{b['anhang_id']}/inhalt", headers=h)
    assert anhang.headers["content-type"].startswith("application/pdf") and len(PdfReader(io.BytesIO(anhang.content)).pages) == 2
    # identische Seiten erneut (eine Sekunde später: das PDF trägt einen Zeitstempel, die Kennung hängt an den Fotos): 409 statt zweitem Beleg
    import time
    time.sleep(1.1)
    assert c.post("/belege/scan", headers=h, files=seiten, data={"kategorie": "fremdleistung", "notiz": "Rechnung Elektriker"}).status_code == 409
    assert c.post("/belege/scan", headers=h, files=seiten, data={"kategorie": "unbekannt"}).status_code == 422
    assert len(c.get("/belege", headers=h).json()) == 1


def test_scan_to_mail_nimmt_nur_erlaubte_absender_und_ist_idempotent(betrieb):
    c, db, ids, h = betrieb
    nachrichten = [scan_mail(), scan_mail(absender="fremd@example.net", message_id="<x@example.net>"),
                   scan_mail(message_id="<scan-2@betrieb.test>", anhaenge=(("a.pdf", "application/pdf"), ("b.jpg", "image/jpeg")))]
    with db.sitzungen() as s:
        m = s.get(Mandant, ids["mandant"])
        pf = Postfach(nachrichten)
        ergebnis = scanner.postfach_scans(s, m, pf, DEMO_ZEIT, "@betrieb.test")
        assert ergebnis == {"gelesen": 3, "uebernommen": 3, "bekannt": 0, "verworfen": 1} and pf.gelesen == [0, 1, 2]
        assert scanner.postfach_scans(s, m, Postfach(nachrichten), DEMO_ZEIT, "@betrieb.test")["bekannt"] == 3
        s.commit()
        belege_ = s.scalars(select(Beleg).where(Beleg.mandant_id == m.id)).all()
        assert len(belege_) == 3 and all(b.import_kennung.startswith("mail:<scan-") for b in belege_)
        assert {b.erkennung["eingang"] for b in belege_} == {"scan-to-mail"} and all("scanner@betrieb.test" in b.notiz for b in belege_)
    # ohne Absenderliste zählt jeder Absender; Mail ohne Anhang wird verworfen
    with db.sitzungen() as s:
        m = s.get(Mandant, ids["mandant"])
        r = scanner.postfach_scans(s, m, Postfach([scan_mail(absender="x@y.test", message_id="<n3>"), scan_mail(message_id="<n4>", anhaenge=())]), DEMO_ZEIT, None)
        assert r["uebernommen"] == 1 and r["verworfen"] == 1


def test_eingangsordner_verschiebt_verarbeitete_dateien(betrieb, tmp_path):
    c, db, ids, h = betrieb
    eingang = tmp_path / "scans"
    eingang.mkdir()
    (eingang / "SCAN_0001.pdf").write_bytes(scanner.bilder_zu_pdf([foto()]))
    (eingang / "SCAN_0002.jpg").write_bytes(foto((10, 10, 10)))
    (eingang / "notiz.txt").write_text("kein Beleg")
    (eingang / ".sync").write_text("")
    with db.sitzungen() as s:
        m = s.get(Mandant, ids["mandant"])
        r = scanner.ordner_scans(s, m, eingang, DEMO_ZEIT)
        s.commit()
    assert r == {"gefunden": 3, "uebernommen": 2, "bekannt": 0, "verworfen": 1}
    assert sorted(p.name for p in eingang.iterdir()) == [".sync", "fehler", "verarbeitet"]
    assert len(list((eingang / "verarbeitet").iterdir())) == 2 and (eingang / "fehler" / "notiz.txt").exists()
    # dieselbe Datei nochmals: bekannt, trotzdem weggeräumt
    (eingang / "SCAN_0001.pdf").write_bytes(scanner.bilder_zu_pdf([foto()]))
    with db.sitzungen() as s:
        assert scanner.ordner_scans(s, s.get(Mandant, ids["mandant"]), eingang, DEMO_ZEIT)["bekannt"] == 1
    assert not (eingang / "SCAN_0001.pdf").exists()
    with pytest.raises(FileNotFoundError):
        with db.sitzungen() as s:
            scanner.ordner_scans(s, s.get(Mandant, ids["mandant"]), tmp_path / "fehlt", DEMO_ZEIT)


def test_scanner_einstellungen_und_abruf_ueber_api(betrieb, tmp_path, monkeypatch):
    c, db, ids, h = betrieb
    from core.geheimnisse import schluessel_erzeugen
    monkeypatch.setenv("INTEGRATIONS_KEY", schluessel_erzeugen())
    assert c.post("/belege/scaneingang/abrufen", headers=h).status_code == 409  # nichts hinterlegt
    eingang = tmp_path / "eingang"
    eingang.mkdir()
    antwort = c.put("/betrieb/scanner", headers=h, json={"host": "imap.betrieb.test", "benutzer": "scan", "passwort": "pw", "erlaubte_absender": "@betrieb.test",
                                                          "eingangsordner": str(eingang), "kategorie": "material"})
    assert antwort.status_code == 200 and antwort.json()["passwort"] == "••••" and antwort.json()["uebergabekanaele"] == {}
    with db.sitzungen() as s:
        assert s.get(Mandant, ids["mandant"]).integrationen["scanner"]["passwort"] != "pw"  # verschlüsselt
    # Passwort bleibt beim Speichern mit Maske erhalten
    c.put("/betrieb/scanner", headers=h, json={"host": "imap.betrieb.test", "benutzer": "scan", "passwort": "••••", "eingangsordner": str(eingang)})
    from core import anfragen
    monkeypatch.setattr(anfragen, "ImapPostfach", lambda *a, **k: type("P", (), {"ungelesene": lambda self: [scan_mail()], "als_gelesen": lambda self, i: None,
                                                                              "schliessen": lambda self: None})())
    (eingang / "beleg.pdf").write_bytes(scanner.bilder_zu_pdf([foto((1, 2, 3))]))
    r = c.post("/belege/scaneingang/abrufen", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["postfach"]["uebernommen"] == 1 and r.json()["ordner"]["uebernommen"] == 1
    liste = c.get("/belege", headers=h).json()
    assert {b["erkennung"]["eingang"] for b in liste} == {"scan-to-mail", "eingangsordner"}
    assert c.get("/betrieb/scanner", headers=h).json()["eingangsordner"] == str(eingang)


def beleg_anlegen(c, h, **felder) -> dict:
    daten = {"lieferant": "Sanitär Großhandel", "nummer": "GH-4711", "datum": "2026-11-10", "brutto": "238.00", "ust_satz": "19", "kategorie": "material", **felder}
    antwort = c.post("/belege", headers=h, files={"datei": ("rechnung.pdf", scanner.bilder_zu_pdf([foto()]), "application/pdf")}, data=daten)
    assert antwort.status_code == 201, antwort.text
    return antwort.json()


def test_pruefung_loest_uebergabe_an_lexware_und_belegtransfer_aus(betrieb, tmp_path):
    c, db, ids, h = betrieb
    assert c.put("/integrationen/finanz", headers=h, json={"kanaele": ["lexware"], "lexware": {"api_key": "lx-key"}}).status_code == 200
    assert c.put("/integrationen/kanzlei", headers=h, json={"kanaele": ["belegtransfer"], "belegtransfer": {"verzeichnis": str(tmp_path / "datev")}}).status_code == 200
    assert c.get("/betrieb/scanner", headers=h).json()["uebergabekanaele"] == {"finanz": ["lexware"], "kanzlei": ["belegtransfer"]}
    b = beleg_anlegen(c, h)
    assert c.post(f"/belege/{b['id']}/uebergabe", headers=h).status_code == 409  # erst prüfen
    assert c.patch(f"/belege/{b['id']}", headers=h, json={"status": "geprueft"}).status_code == 200
    with db.sitzungen() as s:
        offen = s.scalars(select(OutboxEintrag).where(OutboxEintrag.aktion == "beleg_uebermitteln")).all()
        assert sorted(e.ziel for e in offen) == ["finanz", "kanzlei"]
    aufrufe = []

    def lexware(r: httpx.Request) -> httpx.Response:
        aufrufe.append(r)
        assert r.url.path == "/v1/files" and r.headers["Authorization"] == "Bearer lx-key"
        assert r.headers["Content-Type"].startswith("multipart/form-data") and b'name="type"\r\n\r\nvoucher' in r.content
        assert b"2026-11-10_Sanitaer_Grosshandel" in r.content and b"GH_4711.pdf" in r.content
        return httpx.Response(202, json={"id": "file-9"})

    register = Register(tmp_path / "csv", {"transport": httpx.MockTransport(lexware)})
    with db.sitzungen() as s:
        assert abarbeiten(s, register, DEMO_ZEIT)["zugestellt"] == 2
        assert abarbeiten(s, register, DEMO_ZEIT)["zugestellt"] == 0  # nichts doppelt
    assert len(aufrufe) == 1
    ablage = tmp_path / "datev" / "12345_67890" / "Rechnungseingang" / "2026-11"
    assert (ablage / "2026-11-10_Sanitaer_Grosshandel_GH_4711.pdf").exists()
    index = (ablage / "belege.csv").read_text(encoding="utf-8-sig").splitlines()
    assert index[0].startswith("Datei;Lieferant") and "Sanitär Großhandel;GH-4711;2026-11-10" in index[1] and "238,00" in index[1]
    b = c.get(f"/belege/{b['id']}", headers=h).json()
    assert b["status"] == "uebergeben"
    assert {(u["zielsystem"], u["externe_id"]) for u in b["uebergaben"]} == {("lexware", "file-9"), ("belegtransfer", "12345_67890/Rechnungseingang/2026-11/2026-11-10_Sanitaer_Grosshandel_GH_4711.pdf")}
    # manueller Anstoß nach Übergabe: idempotent, keine neuen Outbox-Einträge
    assert c.post(f"/belege/{b['id']}/uebergabe", headers=h).status_code == 200
    with db.sitzungen() as s:
        assert len(s.scalars(select(OutboxEintrag).where(OutboxEintrag.aktion == "beleg_uebermitteln")).all()) == 2
        assert len(s.scalars(select(ExternalRef).where(ExternalRef.entitaet == "beleg")).all()) == 2


def test_sevdesk_beleg_als_entwurf_ohne_kontierung(betrieb):
    c, db, ids, h = betrieb
    b = beleg_anlegen(c, h, nummer="S-1")
    aufrufe = []

    def sevdesk(r: httpx.Request) -> httpx.Response:
        aufrufe.append(r)
        if r.url.path.endswith("/Voucher/Factory/uploadTempFile"):
            assert r.headers["Content-Type"].startswith("multipart/form-data")
            return httpx.Response(201, json={"objects": {"filename": "tmp-abc.pdf"}})
        assert r.url.path.endswith("/Voucher/Factory/saveVoucher")
        body = json.loads(r.content)
        assert body["voucher"]["status"] == 50 and body["voucher"]["supplierName"] == "Sanitär Großhandel" and body["filename"] == "tmp-abc.pdf"
        assert body["voucherPosSave"][0]["sumGross"] == 238.0 and "accountingType" not in body["voucherPosSave"][0]
        return httpx.Response(201, json={"objects": {"voucher": {"id": 555}}})

    adapter = SevdeskAdapter({"api_token": "tok"}, transport=httpx.MockTransport(sevdesk))
    with db.sitzungen() as s:
        m = s.get(Mandant, ids["mandant"])
        beleg = s.get(Beleg, b["id"])
        beleg.status = "geprueft"
        m.integrationen = {**m.integrationen, "finanz": {"kanaele": ["sevdesk"], "sevdesk": {"api_token": "tok"}}}
        assert [e.ziel for e in scanner.uebergabe_beantragen(s, m, beleg)] == ["finanz"]
        s.commit()
        assert abarbeiten(s, {"finanz": adapter}, DEMO_ZEIT)["zugestellt"] == 1
        assert s.get(Beleg, b["id"]).status == "uebergeben"
    assert [r.url.path.rsplit("/", 1)[-1] for r in aufrufe] == ["uploadTempFile", "saveVoucher"]
    u = c.get(f"/belege/{b['id']}", headers=h).json()["uebergaben"][0]
    assert (u["zielsystem"], u["externe_id"]) == ("sevdesk", "555") and u["zeitpunkt"]


def test_upload_mail_nur_mit_upload_adresse(betrieb):
    c, db, ids, h = betrieb
    b = beleg_anlegen(c, h, nummer="UM-7")
    gesendet = []
    ohne = EmailAdapter({"absender": "buero@betrieb.test", "kanzlei_email": "kanzlei@example.test"}, versand=gesendet.append)
    mit = EmailAdapter({"absender": "buero@betrieb.test", "upload_email": "upload-xyz@datev.example"}, versand=gesendet.append)
    with db.sitzungen() as s:
        m = s.get(Mandant, ids["mandant"])
        m.integrationen = {**m.integrationen, "kanzlei": {"kanaele": ["email"], "email": {"absender": "buero@betrieb.test", "host": "smtp", "kanzlei_email": "k@x"}}}
        assert scanner.uebergabekanaele(m) == {}  # ohne upload_email kein Übergabekanal
        m.integrationen = {**m.integrationen, "kanzlei": {"kanaele": ["email"], "email": {"absender": "buero@betrieb.test", "host": "smtp", "upload_email": "upload-xyz@datev.example"}}}
        assert scanner.uebergabekanaele(m) == {"kanzlei": ["email"]}
        beleg = s.get(Beleg, b["id"])
        beleg.status = "geprueft"
        scanner.uebergabe_beantragen(s, m, beleg)
        s.commit()
        e = s.scalar(select(OutboxEintrag).where(OutboxEintrag.aktion == "beleg_uebermitteln"))
        assert ohne.zustellen(e, s) is None and not gesendet
        assert abarbeiten(s, {"kanzlei": mit}, DEMO_ZEIT)["zugestellt"] == 1
    n = gesendet[0]
    assert n["To"] == "upload-xyz@datev.example" and "UM-7" in n["Subject"]
    anhaenge = [t for t in n.iter_attachments()]
    assert len(anhaenge) == 1 and anhaenge[0].get_filename().endswith("UM_7.pdf") and anhaenge[0].get_content_type() == "application/pdf"
    assert c.get(f"/belege/{b['id']}", headers=h).json()["status"] == "uebergeben"


def test_konfigurationspruefung_und_register_fuer_belegtransfer(tmp_path):
    konfiguration_pruefen("kanzlei", {"kanaele": ["belegtransfer"], "belegtransfer": {"verzeichnis": str(tmp_path)}})
    with pytest.raises(ValueError):
        konfiguration_pruefen("kanzlei", {"kanaele": ["belegtransfer"], "belegtransfer": {}})
    with pytest.raises(ValueError):
        konfiguration_pruefen("finanz", {"kanaele": ["belegtransfer"], "belegtransfer": {"verzeichnis": str(tmp_path)}})
    adapter = Register(tmp_path).bauen("belegtransfer", {"verzeichnis": str(tmp_path), "belegtyp": "Rechnungseingang"})
    assert isinstance(adapter, BelegtransferAdapter) and adapter.belegtyp == "Rechnungseingang"
    assert LexwareAdapter({"api_key": "k"}).dateien.headers.get("Content-Type") is None  # Multipart ohne JSON-Kopf
