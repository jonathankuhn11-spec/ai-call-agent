"""Zweite Runde Betriebsmodule: Artikelstamm und Angebote (Annahme → Auftrag → Rechnung), offene Posten mit
Zahlungen, Bankimport und Mahnstufen, Wartungsverträge mit Abrechnungslauf, Abwesenheiten in der Disposition,
Anfrageeingang (Webformular, Postfach), Bewertungsanfrage nach Rapport, Auswertungen und Tageslauf."""
from datetime import timedelta

import pytest

from core import anfragen
from core.demo import DEMO_ZEIT
from core.modelle import Mandant, Rechnung
from tests.test_auth import token
from tests.test_auth import umgebung as oidc_umgebung  # noqa: F401
from tests.test_finanz import STAMMDATEN, rechnung_stellen

HEUTE = DEMO_ZEIT.date()


@pytest.fixture
def betrieb(umgebung):
    c, db, ids, h = umgebung
    assert c.put("/betrieb/stammdaten", headers=h, json={**STAMMDATEN, "bewertungslink": "https://g.page/r/demo/review"}).status_code == 200
    return c, db, ids, h


def freigeben(c, h, f, wer="inhaber"):
    a = c.post(f"/freigaben/{f['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": wer})
    assert a.status_code == 200, a.text
    return a.json()


def artikel_anlegen(c, h):
    a = c.post("/artikel", headers=h, json={"nummer": "A-100", "bezeichnung": "Monteurstunde", "kategorie": "arbeit", "einheit": "Std",
                                           "verkauf_netto": 69, "einkauf_netto": 55})
    assert a.status_code == 201, a.text
    b = c.post("/artikel", headers=h, json={"nummer": "M-200", "bezeichnung": "Umwälzpumpe Hocheffizienz", "verkauf_netto": "289.00", "lieferant": "Großhandel"})
    assert b.status_code == 201
    assert c.post("/artikel", headers=h, json={"nummer": "M-200", "bezeichnung": "Doppelt", "verkauf_netto": 1}).status_code == 409
    return a.json(), b.json()


def angebot_anlegen(c, h, ids):
    arbeit, pumpe = artikel_anlegen(c, h)
    a = c.post("/angebote", headers=h, json={"kunde_id": ids["kunde"], "titel": "Pumpentausch",
                                            "positionen": [{"artikel_id": pumpe["id"], "menge": 1}, {"artikel_id": arbeit["id"], "menge": 2.5},
                                                           {"bezeichnung": "Anfahrt", "menge": 1, "einheit": "psch", "einzelpreis_netto": "35.00"}]})
    assert a.status_code == 201, a.text
    return a.json()


def test_angebot_aus_artikeln_mit_nummernkreis_und_pdf(betrieb):
    c, db, ids, h = betrieb
    a = angebot_anlegen(c, h, ids)
    assert a["nummer"] == "AN-2026-00001" and a["status"] == "entwurf"
    assert a["netto"] == "496.50" and a["brutto"] == "590.84"  # 289 + 2.5*69 + 35 = 496.50; USt 94.34
    assert a["gueltig_bis"] == (HEUTE + timedelta(days=30)).isoformat()
    assert a["positionen"][0]["artikel_id"] and a["positionen"][1]["menge"] == "2.5"
    pdf = c.get(f"/angebote/{a['id']}/pdf", headers=h)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    # Entwurf ist änderbar, Summen werden neu gerechnet
    g = c.patch(f"/angebote/{a['id']}", headers=h, json={"positionen": [{"bezeichnung": "Pauschale", "menge": 1, "einzelpreis_netto": "100.00"}]})
    assert g.status_code == 200 and g.json()["brutto"] == "119.00"
    assert c.post("/angebote", headers=h, json={"kunde_id": ids["kunde"]}).json()["nummer"] == "AN-2026-00002"


def test_angebot_versand_annahme_auftrag_rechnung(betrieb):
    c, db, ids, h = betrieb
    a = angebot_anlegen(c, h, ids)
    f = c.post(f"/angebote/{a['id']}/senden", headers=h).json()
    assert f["risikoklasse"] == "B" and f["aktion"] == "angebot_senden"
    freigeben(c, h, f, "buero")
    assert c.get(f"/angebote/{a['id']}", headers=h).json()["status"] == "versendet"
    outbox = c.get("/outbox", headers=h).json()
    assert any(e["aktion"] == "angebot_zustellen" for e in outbox)
    assert c.patch(f"/angebote/{a['id']}", headers=h, json={"titel": "x"}).status_code == 409  # preisgebunden
    # Annahme erzeugt den Auftrag als Servicefall und eine Auftragsbestätigung (Freigabe B)
    fall = c.post(f"/angebote/{a['id']}/annehmen", headers=h).json()
    assert fall["status"] == "qualifiziert" and fall["quelle"] == "angebot" and "AN-2026-00001" in fall["anliegen"]
    assert c.post(f"/angebote/{a['id']}/annehmen", headers=h).status_code == 409
    bestaetigung = [x for x in c.get("/freigaben", headers=h).json() if x["aktion"] == "nachricht_an_endkunde"]
    assert bestaetigung and "Auftragsbestätigung" in bestaetigung[0]["payload"]["betreff"]
    # Rechnung aus dem Angebot: Klasse C, Büro darf nicht entscheiden
    f = c.post(f"/angebote/{a['id']}/rechnung", headers=h).json()
    assert f["risikoklasse"] == "C" and f["payload"]["angebot_id"] == a["id"]
    freigeben(c, h, f, "inhaber")
    a2 = c.get(f"/angebote/{a['id']}", headers=h).json()
    r = c.get(f"/rechnungen/{a2['rechnung_id']}", headers=h).json()
    assert r["brutto"] == a["brutto"] and r["servicefall_id"] == fall["id"] and len(r["positionen"]) == 3
    assert c.post(f"/angebote/{a['id']}/rechnung", headers=h).status_code == 409


def test_angebot_ablehnung_und_ablauf_im_tageslauf(betrieb):
    c, db, ids, h = betrieb
    a = angebot_anlegen(c, h, ids)
    b = c.post("/angebote", headers=h, json={"kunde_id": ids["kunde"], "gueltig_bis": (HEUTE - timedelta(days=1)).isoformat(),
                                            "positionen": [{"bezeichnung": "Pauschale", "menge": 1, "einzelpreis_netto": "10.00"}]}).json()
    assert c.post(f"/angebote/{a['id']}/senden", headers=h).status_code == 201
    leer = c.post("/angebote", headers=h, json={"kunde_id": ids["kunde"]}).json()
    assert c.post(f"/angebote/{leer['id']}/senden", headers=h).status_code == 422  # ohne Positionen kein Versand
    freigeben(c, h, c.post(f"/angebote/{b['id']}/senden", headers=h).json(), "buero")
    assert c.post(f"/angebote/{a['id']}/ablehnen", headers=h, json={"grund": "zu teuer"}).json()["status"] == "abgelehnt"
    lauf = c.post("/tagesaufgaben", headers=h).json()
    assert lauf["angebote_abgelaufen"] == 1
    assert c.get(f"/angebote/{b['id']}", headers=h).json()["status"] == "abgelaufen"
    quote = c.get("/auswertungen", headers=h).json()["angebote"]
    assert quote["angebote"] == 3 and quote["entschieden"] == 2 and quote["quote"] == 0


def test_zahlungen_teilzahlung_und_offene_posten(betrieb):
    c, db, ids, h = betrieb
    r = rechnung_stellen(c, h, ids, positionen=[{"bezeichnung": "Wartung", "menge": 1, "einzelpreis_netto": "200.00"}])
    assert r["brutto"] == "238.00"
    op = c.get("/offene-posten", headers=h).json()
    assert len(op) == 1 and op[0]["offen"] == "238.00" and op[0]["mahnstufe"] == 0
    z = c.post("/zahlungen", headers=h, json={"rechnung_id": r["id"], "betrag": 100, "art": "ueberweisung", "referenz": "Teil 1"})
    assert z.status_code == 201, z.text
    assert c.get(f"/rechnungen/{r['id']}", headers=h).json()["status"] == "teilbezahlt"
    assert c.get("/offene-posten", headers=h).json()[0]["offen"] == "138.00"
    c.post("/zahlungen", headers=h, json={"rechnung_id": r["id"], "betrag": 138, "art": "bar"})
    assert c.get(f"/rechnungen/{r['id']}", headers=h).json()["status"] == "bezahlt"
    assert c.get("/offene-posten", headers=h).json() == []
    assert len(c.get("/zahlungen", headers=h, params={"rechnung_id": r["id"]}).json()) == 2
    assert c.post("/zahlungen", headers=h, json={"rechnung_id": r["id"], "betrag": 0}).status_code == 422


def test_bankimport_ordnet_rechnungsnummern_zu(betrieb):
    c, db, ids, h = betrieb
    r1 = rechnung_stellen(c, h, ids, positionen=[{"bezeichnung": "A", "menge": 1, "einzelpreis_netto": "100.00"}])
    r2 = rechnung_stellen(c, h, ids, positionen=[{"bezeichnung": "B", "menge": 1, "einzelpreis_netto": "50.00"}])
    csv = ("Buchungstag;Wertstellung;Verwendungszweck;Betrag;Währung\n"
           f"17.11.2026;17.11.2026;Rechnung {r1['nummer']} Familie Beispiel;119,00;EUR\n"
           f"16.11.2026;16.11.2026;{r2['nummer'].replace('-', '')} Abschlag;30,00;EUR\n"
           "15.11.2026;15.11.2026;Miete Werkstatt;-1200,00;EUR\n"
           "15.11.2026;15.11.2026;Überweisung ohne Bezug;80,00;EUR\n").encode("cp1252")
    antwort = c.post("/zahlungen/bankimport", headers=h, files={"datei": ("umsaetze.csv", csv, "text/csv")})
    assert antwort.status_code == 200, antwort.text
    e = antwort.json()
    assert len(e["zugeordnet"]) == 2 and len(e["unklar"]) == 1 and e["uebersprungen"] >= 1
    assert c.get(f"/rechnungen/{r1['id']}", headers=h).json()["status"] == "bezahlt"
    assert c.get(f"/rechnungen/{r2['id']}", headers=h).json()["status"] == "teilbezahlt"
    # erneuter Import derselben Datei bucht nichts doppelt
    e2 = c.post("/zahlungen/bankimport", headers=h, files={"datei": ("umsaetze.csv", csv, "text/csv")}).json()
    assert e2["zugeordnet"] == [] and len(c.get("/zahlungen", headers=h).json()) == 2


def test_mahnstufen_entstehen_nur_ueber_freigaben(betrieb):
    c, db, ids, h = betrieb
    r = rechnung_stellen(c, h, ids, positionen=[{"bezeichnung": "A", "menge": 1, "einzelpreis_netto": "100.00"}])
    with db.sitzungen() as s:
        s.get(Rechnung, r["id"]).faellig = HEUTE - timedelta(days=20)
        s.commit()
    v = c.post("/mahnlauf", headers=h).json()
    assert len(v) == 1 and v[0]["stufe"] == 1
    assert c.post("/mahnlauf", headers=h).json() == []  # keine Doppelvorschläge bei offener Freigabe
    f = c.get("/freigaben", headers=h).json()
    erinnerung = next(x for x in f if x["aktion"] == "zahlungserinnerung_senden")
    assert erinnerung["risikoklasse"] == "B"
    freigeben(c, h, erinnerung, "buero")
    m = c.get("/mahnungen", headers=h).json()
    assert len(m) == 1 and m[0]["stufe"] == 1 and m[0]["gebuehr"] == "0.00" and m[0]["offen"] == "119.00"
    assert c.get(f"/mahnungen/{m[0]['id']}/pdf", headers=h).content.startswith(b"%PDF")
    assert any(e["aktion"] == "mahnung_zustellen" for e in c.get("/outbox", headers=h).json())
    assert c.get("/offene-posten", headers=h).json()[0]["mahnstufe"] == 1
    # Stufe 2 erst nach Ablauf der Mahnfrist; dann Klasse C mit Gebühr (Freigabewirkung datiert mit der Systemuhr, daher Frist setzen)
    from sqlalchemy import select

    from core.modelle import Mahnung
    with db.sitzungen() as s:
        s.scalar(select(Mahnung)).frist = HEUTE + timedelta(days=5)
        s.commit()
    assert c.post("/mahnlauf", headers=h).json() == []
    with db.sitzungen() as s:
        s.scalar(select(Mahnung)).frist = HEUTE - timedelta(days=1)
        s.commit()
    v = c.post("/mahnlauf", headers=h).json()
    assert v and v[0]["stufe"] == 2
    f2 = next(x for x in c.get("/freigaben", headers=h).json() if x["aktion"] == "mahnung_senden")
    assert f2["risikoklasse"] == "C" and f2["payload"]["gebuehr"] == "5.00"
    # Zahlung vor der Entscheidung: Freigabe läuft ins Leere statt zu mahnen
    c.post("/zahlungen", headers=h, json={"rechnung_id": r["id"], "betrag": 119})
    assert c.post(f"/freigaben/{f2['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "inhaber"}).status_code in (200, 409)
    assert len(c.get("/mahnungen", headers=h).json()) == 1


def test_wartungsvertrag_laufzeit_kuendigung_abrechnung(betrieb):
    c, db, ids, h = betrieb
    v = c.post("/vertraege", headers=h, json={"kunde_id": ids["kunde"], "anlagen_ids": [ids["anlage"]], "preis_jahr_netto": 180,
                                             "beginn": "2026-01-01", "leistungen": "jährliche Wartung der Gastherme", "naechste_abrechnung": "2026-11-01"})
    assert v.status_code == 201, v.text
    v = v.json()
    assert v["laufzeitende"] == "2026-12-31" and v["kuendigungstermin"] == "2026-09-30" and v["status"] == "aktiv"
    assert c.get(f"/kunden/{ids['kunde']}", headers=h).json()["kunde"]["vertragsstatus"] == "wartungsvertrag"
    fristen = c.get("/fristen", headers=h).json()
    assert any(f["art"] == "vertrag" and f["faellig"] == "2026-09-30" for f in fristen)
    # Abrechnung fällig (01.11. < 17.11.): Tageslauf legt Freigabe C an, nächste Abrechnung ein Jahr weiter
    lauf = c.post("/tagesaufgaben", headers=h).json()
    assert len(lauf["vertragsabrechnungen"]) == 1
    f = next(x for x in c.get("/freigaben", headers=h).json() if x["aktion"] == "rechnung_erstellen" and x["payload"].get("vertrag_id"))
    freigeben(c, h, f, "inhaber")
    r = c.get("/rechnungen", headers=h).json()[0]
    assert r["netto"] == "180.00"
    assert c.get(f"/vertraege/{v['id']}", headers=h).json()["naechste_abrechnung"] == "2027-11-01"
    assert c.post("/tagesaufgaben", headers=h).json()["vertragsabrechnungen"] == []
    # Kündigung nach dem Kündigungstermin wirkt erst zum Ende der Verlängerung
    k = c.post(f"/vertraege/{v['id']}/kuendigen", headers=h, json={}).json()
    assert k["status"] == "gekuendigt" and k["gekuendigt_zum"] == "2027-12-31"
    assert c.post(f"/vertraege/{v['id']}/kuendigen", headers=h, json={}).status_code == 422
    assert c.get("/auswertungen", headers=h).json()["vertraege"]["aktive"] == 0


def test_abwesenheit_blockiert_disposition(betrieb):
    c, db, ids, h = betrieb
    tag = HEUTE + timedelta(days=1)
    a = c.post("/abwesenheiten", headers=h, json={"ressource_id": ids["monteur_gas"], "von": tag.isoformat(), "bis": tag.isoformat(), "art": "urlaub"})
    assert a.status_code == 201, a.text
    assert c.post("/abwesenheiten", headers=h, json={"ressource_id": ids["monteur_gas"], "von": "2026-12-02", "bis": "2026-12-01"}).status_code == 422
    fenster = c.get("/disposition/zeitfenster", headers=h, params={"qualifikation": "gas", "ab": f"{tag.isoformat()}T07:00:00+01:00", "tage": 2}).json()
    assert fenster and all(f["beginn"][:10] != tag.isoformat() for f in fenster if f["ressource_id"] == ids["monteur_gas"])
    tafel = c.get("/plantafel", headers=h, params={"tag": tag.isoformat(), "tage": 1}).json()
    zeile = next(z for z in tafel["ressourcen"] if z["ressource_id"] == ids["monteur_gas"])
    assert zeile["abwesenheiten"] and zeile["abwesenheiten"][0]["art"] == "urlaub"
    assert c.delete(f"/abwesenheiten/{a.json()['id']}", headers=h).status_code == 204
    assert c.get("/abwesenheiten", headers=h).json() == []


def test_webformular_oeffentlich_mit_token_und_honeypot(betrieb):
    c, db, ids, h = betrieb
    w = c.put("/betrieb/webformular", headers=h, json={"aktiv": True}).json()
    assert w["aktiv"] and "/anfragen/web/" in w["url"] and "<form" in w["snippet"]
    token = w["url"].rsplit("/", 1)[1]
    # ohne Login, Formularfelder wie von der Website
    a = c.post(f"/anfragen/web/{token}", data={"name": "Erika Neu", "telefon": "0221 9988776", "email": "erika@example.test",
                                                "nachricht": "Es riecht nach Gas im Keller, bitte dringend melden", "website": ""})
    assert a.status_code == 200, a.text
    anfr = c.get("/anfragen", headers=h).json()
    assert len(anfr) == 1 and anfr[0]["status"] == "uebernommen" and anfr[0]["kunde_id"] and anfr[0]["servicefall_id"]
    fall = c.get(f"/servicefaelle/{anfr[0]['servicefall_id']}", headers=h).json()["servicefall"]
    assert fall["dringlichkeit"] == "notdienst" and fall["quelle"] == "web"
    assert any(f["aktion"] == "nachricht_an_endkunde" and "Sicherheits" in f["payload"]["betreff"] for f in c.get("/freigaben", headers=h).json())
    kunden = c.get("/kunden", headers=h, params={"q": "Erika"}).json()
    assert kunden and kunden[0]["telefon"] == "+492219988776"
    # Honeypot ausgefüllt: still verworfen; falscher Token: 404; abgeschaltet: 404
    assert c.post(f"/anfragen/web/{token}", data={"name": "Bot", "telefon": "1", "nachricht": "x" * 20, "website": "http://spam"}).status_code == 200
    assert len(c.get("/anfragen", headers=h).json()) == 1
    assert c.post(f"/anfragen/web/{ids['mandant']}.falsch", data={"telefon": "1", "nachricht": "hallo welt"}).status_code == 404
    c.put("/betrieb/webformular", headers=h, json={"aktiv": False})
    assert c.post(f"/anfragen/web/{token}", data={"telefon": "1", "nachricht": "hallo welt"}).status_code == 404


def test_postfach_wird_zu_vorgaengen(betrieb):
    c, db, ids, h = betrieb

    class Postfach:
        def __init__(self):
            roh = ("From: Familie Beispiel <fam@example.test>\r\nTo: info@example.test\r\nSubject: Therme tropft\r\n"
                   "Message-ID: <abc123@example.test>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
                   "Guten Tag, unsere Therme tropft seit gestern. Rückruf bitte unter 0221 1234567.\r\n").encode()
            self.nachrichten = [roh]
            self.markiert = []

        def ungelesene(self):
            return list(self.nachrichten)

        def als_gelesen(self, kennung):
            self.markiert.append(kennung)

    with db.sitzungen() as s:
        m = s.get(Mandant, ids["mandant"])
        pf = Postfach()
        e = anfragen.postfach_abrufen(s, m, pf, DEMO_ZEIT)
        s.commit()
    assert e["uebernommen"] == 1 and pf.markiert == [0]
    a = c.get("/anfragen", headers=h).json()[0]
    assert a["kanal"] == "email" and a["externe_id"] == "<abc123@example.test>" and a["kunde_id"] == ids["kunde"]  # Bestandskunde per Telefon erkannt
    with db.sitzungen() as s:
        assert anfragen.postfach_abrufen(s, s.get(Mandant, ids["mandant"]), pf, DEMO_ZEIT)["bekannt"] == 1  # idempotent je Message-ID
    assert c.post("/posteingang/abrufen", headers=h).status_code == 409  # kein Postfach konfiguriert
    p = c.put("/betrieb/posteingang", headers=h, json={"host": "imap.example.test", "benutzer": "info", "passwort": "geheim"}).json()
    assert p["passwort"] == "••••" and c.get("/betrieb/posteingang", headers=h).json()["host"] == "imap.example.test"


def test_bewertungsanfrage_nach_erledigtem_rapport(betrieb):
    c, db, ids, h = betrieb
    from tests.test_einsatz import fall_mit_termin
    fall_id, termin = fall_mit_termin(c, h, ids, ids["monteur_gas"])
    r = c.post(f"/servicefaelle/{fall_id}/rapport", headers=h, json={"ressource_id": ids["monteur_gas"], "taetigkeiten": "Pumpe getauscht",
                                                                        "arbeitszeit_minuten": 90, "ergebnis": "erledigt"})
    assert r.status_code == 201, r.text
    f = [x for x in c.get("/freigaben", headers=h).json() if x["aktion"] == "bewertung_anfragen"]
    assert len(f) == 1 and f[0]["risikoklasse"] == "B" and "review" in f[0]["payload"]["text"]
    freigeben(c, h, f[0], "buero")
    assert any(e["aktion"] == "nachricht" and "review" in e["payload"].get("text", "") for e in c.get("/outbox", headers=h).json())


def test_auswertungen_und_rechte(betrieb):
    c, db, ids, h = betrieb
    rechnung_stellen(c, h, ids, positionen=[{"bezeichnung": "A", "menge": 1, "einzelpreis_netto": "100.00"}])
    a = c.get("/auswertungen", headers=h, params={"monate": 3}).json()
    assert len(a["monate"]) == 3 and a["umsatz_netto_gesamt"] == "100.00" and a["offene_posten"]["anzahl"] == 1
    assert set(a) >= {"monate", "offene_posten", "angebote", "vertraege", "monteure", "anfragen_je_kanal"}


def test_rechte_monteur_ohne_finanzen(oidc_umgebung):  # noqa: F811
    c, ids = oidc_umgebung
    monteur = token(ids["mandant"], ["monteur"], name="m.gas")
    assert c.get("/auswertungen", headers=monteur).status_code == 403
    assert c.get("/offene-posten", headers=monteur).status_code == 403
    assert c.get("/anfragen", headers=monteur).status_code == 403
    assert c.get("/artikel", headers=monteur).status_code == 200
    assert c.get("/abwesenheiten", headers=monteur).status_code == 200


def test_loeschlauf_und_auskunft_mit_neuen_entitaeten(betrieb):
    c, db, ids, h = betrieb
    from datetime import datetime, timezone

    from core.loeschung import loeschlauf
    a = angebot_anlegen(c, h, ids)
    c.post(f"/angebote/{a['id']}/ablehnen", headers=h, json={})
    c.post("/vertraege", headers=h, json={"kunde_id": ids["kunde"], "preis_jahr_netto": 100, "beginn": "2020-01-01"})
    auskunft = c.get(f"/kunden/{ids['kunde']}/auskunft", headers=h).json()
    assert len(auskunft["angebote"]) == 1 and len(auskunft["vertraege"]) == 1
    assert "laufender Wartungsvertrag" in c.post(f"/kunden/{ids['kunde']}/loeschen", headers=h).text
    with db.sitzungen() as s:
        m = s.get(Mandant, ids["mandant"])
        z = loeschlauf(s, m, datetime(2031, 1, 1, tzinfo=timezone.utc))
        s.commit()
    assert z["angebote_geloescht"] == 1 and z["vertraege_geloescht"] == 0  # aktiver Vertrag bleibt
    assert c.get("/angebote", headers=h).json() == []
