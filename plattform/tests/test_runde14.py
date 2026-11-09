"""Runde 14: Robustheit der öffentlichen Eingänge und des Telefonagenten.

Online-Buchung und Portal-Wunschtermin akzeptieren nur angebotene Zeiten; E-Mail-Eingang verwirft Zustellfehler,
Newsletter und Autoreplies und vergibt prozessübergreifend stabile Kennungen; ein belegter Termin beim Telefonagenten
verwirft den Vorgang nicht mehr; überlange Eingaben werden gekürzt; Drosselung je Absender."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from core import anfragen
from core.demo import DEMO_ZEIT
from core.modelle import Anfrage, Ereignis, Kunde, Mandant, Servicefall
from tests.test_runde4 import STAMMDATEN, friseur


def buchungsseite(c, h):
    c.put("/betrieb/stammdaten", headers=h, json={**STAMMDATEN, "basis_url": "https://salon.example.test"})
    friseur(c, h)
    token = c.put("/betrieb/buchung", headers=h).json()["pfad"].split("#", 1)[1]
    le = next(x for x in c.get(f"/buchung/{token}").json()["leistungen"] if x["nummer"] == "L-310")
    erste = c.get(f"/buchung/{token}/zeiten", params={"leistung_id": le["id"]}).json()["tage"][0]["zeiten"][0]
    return token, le, erste


def test_onlinebuchung_nur_angebotene_zeiten(umgebung):
    c, db, ids, h = umgebung
    token, le, erste = buchungsseite(c, h)
    basis = {"leistung_id": le["id"], "ressource_id": erste["ressource_id"], "name": "Nacht Eule", "telefon": "0221 5550001"}
    sonntag_nachts = (DEMO_ZEIT + timedelta(days=(6 - DEMO_ZEIT.weekday()) % 7 or 7)).replace(hour=3, minute=7)
    r = c.post(f"/buchung/{token}", json={**basis, "beginn": sonntag_nachts.isoformat()})
    assert r.status_code == 409 and "nicht buchbar" in r.json()["detail"]
    assert "Buchungszeitraum" in c.post(f"/buchung/{token}", json={**basis, "beginn": (DEMO_ZEIT + timedelta(days=400)).isoformat()}).json()["detail"]
    farbe = next(x for x in c.get(f"/buchung/{token}").json()["leistungen"] if x["nummer"] == "L-330")   # niemand qualifiziert
    assert c.post(f"/buchung/{token}", json={**basis, "leistung_id": farbe["id"], "beginn": erste["beginn"]}).status_code == 409
    assert c.post(f"/buchung/{token}", json={**basis, "beginn": erste["beginn"]}).status_code == 201
    # dieselbe Zeit ist danach nicht mehr buchbar
    assert c.post(f"/buchung/{token}", json={**basis, "telefon": "0221 5550002", "beginn": erste["beginn"]}).status_code == 409
    assert c.post(f"/buchung/{token}", json={**basis, "plz": "1" * 11, "beginn": erste["beginn"]}).status_code == 422   # Spaltenlänge


def test_portal_wunschtermin_nur_freie_fenster(umgebung):
    c, db, ids, h = umgebung
    fall_id = c.post("/voice/servicefall", headers=h, json={"telefon": "0221 1234567", "name": "Familie Beispiel", "anliegen": "Heizung tropft"}).json()["servicefall_id"]
    token = c.get(f"/servicefaelle/{fall_id}/portal", headers=h).json()["pfad"].rsplit("/", 1)[-1]
    fenster = c.get(f"/portal/faelle/{fall_id}/alternativen?token={token}").json()
    nachts = (DEMO_ZEIT + timedelta(days=2)).replace(hour=2, minute=13)
    r = c.post(f"/portal/faelle/{fall_id}/alternativen?token={token}", json={"ressource_id": fenster[0]["ressource_id"], "beginn": nachts.isoformat()})
    assert r.status_code == 409 and "nicht mehr frei" in r.json()["detail"]
    assert c.post(f"/portal/faelle/{fall_id}/alternativen?token={token}", json={"ressource_id": fenster[0]["ressource_id"], "beginn": fenster[0]["beginn"]}).status_code == 200
    c.post(f"/servicefaelle/{fall_id}/status", headers=h, json={"ziel": "storniert"})
    r = c.post(f"/portal/faelle/{fall_id}/alternativen?token={token}", json={"ressource_id": fenster[1]["ressource_id"], "beginn": fenster[1]["beginn"]})
    assert r.status_code == 409


def mail(kopf: str, text: str = "Bitte Wartung der Heizung, Tel. 0221 3344556") -> bytes:
    return (f"From: Kunde Test <kunde@example.invalid>\r\nSubject: Wartung\r\n{kopf}Content-Type: text/plain; charset=utf-8\r\n\r\n{text}\r\n").encode()


def test_email_eingang_verwirft_automatische_post_und_ist_idempotent(umgebung):
    c, db, ids, h = umgebung
    assert not anfragen.nachricht_lesen(mail(""))["automatisch"]
    for kopf in ("Auto-Submitted: auto-replied\r\n", "Precedence: bulk\r\n", "List-Unsubscribe: <mailto:x@example.invalid>\r\n", "X-Autoreply: yes\r\n"):
        assert anfragen.nachricht_lesen(mail(kopf))["automatisch"], kopf
    bounce = b"From: Mail Delivery System <MAILER-DAEMON@mx.example.invalid>\r\nSubject: Undelivered Mail Returned to Sender\r\n\r\nx\r\n"
    assert anfragen.nachricht_lesen(bounce)["automatisch"]
    # ohne Message-ID: stabile Kennung (sha256), nicht hash()
    a, b = anfragen.nachricht_lesen(mail("")), anfragen.nachricht_lesen(mail(""))
    assert a["externe_id"] == b["externe_id"] and len(a["externe_id"]) == len("ohne-id:") + 40
    html_mail = (b"From: a@example.invalid\r\nSubject: Frage\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
                 b"<style>p{color:red}</style><p>Gr&uuml;&szlig;e &amp; Dank</p>\r\n")
    assert anfragen.nachricht_lesen(html_mail)["nachricht"] == "Grüße & Dank"

    class Postfach:
        def __init__(self, nachrichten):
            self.n = nachrichten

        def ungelesene(self):
            return list(self.n)

        def als_gelesen(self, i):
            pass

    with db.sitzungen() as s:
        m = s.get(Mandant, ids["mandant"])
        vorher = s.query(Kunde).count()
        e1 = anfragen.postfach_abrufen(s, m, Postfach([bounce, mail("List-Unsubscribe: <mailto:x@example.invalid>\r\n"), mail("")]), DEMO_ZEIT)
        e2 = anfragen.postfach_abrufen(s, m, Postfach([mail("")]), DEMO_ZEIT)
        s.commit()
        assert e1 == {"gelesen": 3, "uebernommen": 1, "verworfen": 2, "bekannt": 0} and e2["bekannt"] == 1
        assert s.query(Kunde).count() == vorher + 1   # weder der Mailer-Daemon noch der Newsletter wurden zum Kunden


def test_telefonagent_belegter_termin_behaelt_vorgang(umgebung):
    c, db, ids, h = umgebung
    beginn = (DEMO_ZEIT + timedelta(days=1)).replace(hour=9)
    c.post("/voice/servicefall", headers=h, json={"telefon": "0221 1111111", "name": "Erster", "anliegen": "Wartung", "ressource_id": ids["monteur_gas"], "beginn": beginn.isoformat()})
    r = c.post("/voice/servicefall", headers=h, json={"telefon": "0221 2222222", "name": "Zweiter " + "x" * 300, "anliegen": "Heizung kalt",
                                                      "plz": "123456789012", "ressource_id": ids["monteur_gas"], "beginn": beginn.isoformat()})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["termin"] is None and d["termin_fehler"] and "nicht mehr frei" in d["hinweis"]
    with db.sitzungen() as s:
        f = s.get(Servicefall, d["servicefall_id"])
        assert f is not None and len(f.kunde.name) == 200 and len(f.kunde.plz) == 10
        assert s.scalar(select(Ereignis).where(Ereignis.typ == "termin.nicht_moeglich", Ereignis.entitaet_id == f.id))


def test_oeffentliche_eingaben_werden_gekuerzt(umgebung):
    c, db, ids, h = umgebung
    with db.sitzungen() as s:
        m = s.get(Mandant, ids["mandant"])
        a = anfragen.anfrage_verarbeiten(s, m, "web", "lang-1", "N" * 500, "0" * 80, ("e" * 300) + "@example.invalid", "Betreff", "Bitte Rückruf wegen Wartung",
                                         DEMO_ZEIT, "S" * 300, "1" * 20, "O" * 300)
        s.commit()
        a = s.scalar(select(Anfrage).where(Anfrage.externe_id == "lang-1"))
        assert len(a.name) == 200 and len(a.telefon) <= 32 and len(a.email) == 200
        k = s.get(Kunde, a.kunde_id)
        assert len(k.strasse) == 200 and len(k.plz) == 10 and len(k.ort) == 100


def test_drosselung_je_absender_schuetzt_kontingent_des_betriebs(umgebung):
    c, db, ids, h = umgebung
    c.put("/betrieb/website", headers=h, json={"aktiv": True, "slug": "demo-haustechnik"})
    daten = {"name": "Bot", "telefon": "0221 999", "nachricht": "spam spam spam"}
    codes = [c.post("/web/demo-haustechnik/anfrage", data={**daten, "nachricht": f"Anfrage Nummer {i}"}, headers={"Accept": "application/json"}).status_code for i in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429
    # andere Absenderadresse kommt weiter durch (Kontingent des Betriebs nicht erschöpft)
    from fastapi.testclient import TestClient
    andere = TestClient(c.app, client=("203.0.113.7", 50000))
    assert andere.post("/web/demo-haustechnik/anfrage", data=daten, headers={"Accept": "application/json"}).status_code == 200
