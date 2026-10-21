"""Löschkonzept: Fristen, Löschlauf, Auskunft und Löschantrag."""
from datetime import timedelta

from sqlalchemy import select

from core.demo import DEMO_ZEIT
from core.loeschung import ANONYM, loeschlauf
from core.modelle import Ereignis, Kommunikation, Kunde, Mandant, OutboxEintrag, Rechnung, Servicefall


def test_loeschlauf_setzt_fristen_um(umgebung):
    c, db, ids, h = umgebung
    fall = c.post("/voice/servicefall", headers=h, json={"telefon": "0221 1234567", "name": "Familie Beispiel",
                                                         "anliegen": "Heizung aus"}).json()
    c.post("/voice/gespraech-beendet", headers=h, json={"telefon": "0221 1234567", "servicefall_id": fall["servicefall_id"],
                                                        "transkript": "sehr lang", "zusammenfassung": "kurz"})
    c.post("/voice/gespraech-beendet", headers=h, json={"telefon": "0221 999", "transkript": "Falschwahl"})  # ohne Kunde
    with db.sitzungen() as s:
        m = s.get(Mandant, ids["mandant"])
        vorher = loeschlauf(s, m, DEMO_ZEIT, trocken=True)
        assert vorher["transkripte_entfernt"] == 0 and vorher["kommunikation_geloescht"] == 0
        spaeter = DEMO_ZEIT + timedelta(days=91)
        ergebnis = loeschlauf(s, m, spaeter)
        assert ergebnis["transkripte_entfernt"] == 2 and ergebnis["kommunikation_geloescht"] == 1
        k = s.scalar(select(Kommunikation).where(Kommunikation.servicefall_id == fall["servicefall_id"]))
        assert k.transkript is None and k.zusammenfassung == "kurz"
        assert s.scalar(select(Ereignis).where(Ereignis.typ == "loeschung.lauf")) is not None
        # Vorgang bleibt (3 Jahre), Kunde bleibt (aktiv)
        assert s.get(Servicefall, fall["servicefall_id"]) is not None
        assert s.get(Kunde, ids["kunde"]).name == "Familie Beispiel"
        # nach 3 Jahren: abgeschlossener Vorgang weg, inaktiver Kunde anonymisiert, Rechnungs-Ereignisse bleiben
        f = s.get(Servicefall, fall["servicefall_id"])
        f.status = "storniert"
        s.get(Kunde, ids["kunde"]).vertragsstatus = "ohne"  # mit Wartungsvertrag bliebe der Kunde erhalten
        s.add(Ereignis(mandant_id=m.id, typ="rechnung.gestellt", entitaet="rechnung", entitaet_id="x", akteur="system", zeit=DEMO_ZEIT))
        s.commit()
        viel_spaeter = DEMO_ZEIT + timedelta(days=1100)
        ergebnis = loeschlauf(s, m, viel_spaeter)
        assert ergebnis["vorgaenge_geloescht"] == 1 and ergebnis["kunden_anonymisiert"] == 1
        assert s.get(Kunde, ids["kunde"]).name == ANONYM and s.get(Kunde, ids["kunde"]).telefon is None
        assert s.scalar(select(Ereignis).where(Ereignis.typ == "rechnung.gestellt")) is not None
        assert s.scalar(select(Ereignis).where(Ereignis.typ == "servicefall.eingegangen")) is None


def test_auskunft_und_loeschantrag(umgebung):
    c, db, ids, h = umgebung
    auskunft = c.get(f"/kunden/{ids['kunde']}/auskunft", headers=h).json()
    assert auskunft["kunde"]["name"] == "Familie Beispiel" and auskunft["objekte"][0]["anlagen"][0]["typ"] == "gas_brennwert"
    assert "speicherdauer" in auskunft and any(e["typ"] == "kunde.auskunft" for e in c.get("/ereignisse", headers=h).json())
    # Wartungsvertrag sperrt die Löschung
    assert c.post(f"/kunden/{ids['kunde']}/loeschen", headers=h).status_code == 422
    c.patch(f"/kunden/{ids['kunde']}", headers=h, json={"vertragsstatus": "ohne"})
    # offener Vorgang sperrt ebenfalls
    fall = c.post("/servicefaelle", headers=h, json={"kunde_id": ids["kunde"], "anliegen": "Tropft"}).json()
    assert "offener Vorgang" in c.post(f"/kunden/{ids['kunde']}/loeschen", headers=h).json()["detail"]
    c.post(f"/servicefaelle/{fall['id']}/status", headers=h, json={"ziel": "storniert", "grund": "Kunde wünscht Löschung"})
    ergebnis = c.post(f"/kunden/{ids['kunde']}/loeschen", headers=h).json()
    assert ergebnis["art"] == "vollständig" and ergebnis["vorgaenge"] == 1
    assert c.get(f"/kunden/{ids['kunde']}", headers=h).status_code == 404
    with db.sitzungen() as s:
        assert s.scalar(select(Servicefall)) is None
        assert s.scalar(select(Ereignis).where(Ereignis.typ == "kunde.geloescht")).payload["art"] == "vollständig"


def test_loeschantrag_mit_rechnung_anonymisiert(umgebung):
    c, db, ids, h = umgebung
    c.patch(f"/kunden/{ids['kunde']}", headers=h, json={"vertragsstatus": "ohne", "email": "k@example.test"})
    f = c.post("/freigaben", headers=h, json={"aktion": "rechnung_erstellen", "payload": {"kunde_id": ids["kunde"], "betrag_netto": 10}}).json()
    c.post(f"/freigaben/{f['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "inhaber"})
    ergebnis = c.post(f"/kunden/{ids['kunde']}/loeschen", headers=h).json()
    assert ergebnis["art"] == "anonymisiert" and "Aufbewahrungsfrist" in ergebnis["grund"]
    k = c.get(f"/kunden/{ids['kunde']}", headers=h).json()["kunde"]
    assert k["name"] == ANONYM and k["email"] is None
    with db.sitzungen() as s:
        r = s.scalar(select(Rechnung))
        assert r.empfaenger["name"] == "Familie Beispiel"  # Rechnung behält die Momentaufnahme (§ 14 UStG)
        assert s.scalar(select(OutboxEintrag)) is not None
