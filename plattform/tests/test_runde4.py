"""Runde 4: Branchenprofile (Friseur, Kosmetik, Massage …), Online-Buchung, Kundenkartei, No-Show, Gutscheine, Wiederkehr."""
from datetime import date, timedelta

from sqlalchemy import select

from core import branchen, gutscheine
from core.adapter.email import EmailAdapter
from core.demo import DEMO_ZEIT
from core.modelle import Ereignis, Freigabe, Gutschein, Kunde, Servicefall, Termin
from core.zustellung import abarbeiten
from tests.test_einsatz import fall_mit_termin
from tests.test_finanz import STAMMDATEN


def friseur(c, h):
    r = c.put("/betrieb/branche", headers=h, json={"branche": "friseur"})
    assert r.status_code == 200, r.text
    return r.json()


# --- Branchenprofile -----------------------------------------------------------------------------
def test_branchenliste_und_umstellung_legt_leistungskatalog_an(umgebung):
    c, db, ids, h = umgebung
    liste = c.get("/branchen").json()
    assert len(liste) == 17 and {b["schluessel"] for b in liste} >= {"shk", "friseur", "kosmetik", "massage", "physiotherapie", "hundesalon", "kfz"}
    assert c.get("/betrieb/branche", headers=h).json()["schluessel"] == "shk"      # Standard aus gewerk
    u = friseur(c, h)
    assert u["profil"]["schluessel"] == "friseur" and u["profil"]["begriffe"]["ressource"] == "Mitarbeiter/in" and u["profil"]["begriffe"]["anlagen"] is False
    assert u["leistungen_angelegt"] == 9
    leistungen = [a for a in c.get("/artikel?alle=true", headers=h).json() if a["kategorie"] == "leistung"]
    assert len(leistungen) == 9 and {a["nummer"] for a in leistungen} >= {"L-300", "L-330"}
    faerben = next(a for a in leistungen if a["nummer"] == "L-330")
    assert faerben["dauer_minuten"] == 90 and faerben["qualifikation"] == "farbe" and faerben["verkauf_netto"] == "57.98"
    # nochmal umstellen legt nichts doppelt an
    assert friseur(c, h)["leistungen_angelegt"] == 0
    assert c.put("/betrieb/branche", headers=h, json={"branche": "zahnarzt"}).status_code == 422
    with db.sitzungen() as s:
        assert s.scalar(select(Ereignis).where(Ereignis.typ == "betrieb.branche")).payload["branche"] == "friseur"
    k = c.get("/voice/kontext", headers=h).json()
    assert "Friseursalon" in k["rolle"] and k["anlagen"] is False and k["sicherheitsfaelle"] is False and len(k["leistungen"]) == 9


def test_kundenkartei_nur_bekannte_felder_und_gesundheitsdaten_mit_einwilligung(umgebung):
    c, db, ids, h = umgebung
    friseur(c, h)
    kid = ids["kunde"]
    assert c.put(f"/kunden/{kid}/kartei", headers=h, json={"felder": {"rezeptur": "Wella 7/1 + 6% 35 min", "stammstylist": "Mia"}}).json()["kartei"] == \
        {"rezeptur": "Wella 7/1 + 6% 35 min", "stammstylist": "Mia"}
    assert c.put(f"/kunden/{kid}/kartei", headers=h, json={"felder": {"schuhgroesse": "42"}}).status_code == 422
    r = c.put(f"/kunden/{kid}/kartei", headers=h, json={"felder": {"rezeptur": "x", "allergien": "PPD"}})
    assert r.status_code == 409 and "gesundheitsdaten" in r.json()["detail"]
    assert c.put(f"/kunden/{kid}/einwilligungen", headers=h, json={"gesundheitsdaten": True}).status_code == 200
    r = c.put(f"/kunden/{kid}/kartei", headers=h, json={"felder": {"rezeptur": "x", "allergien": "PPD", "allergietest": "2026-10-01"}})
    assert r.status_code == 200 and r.json()["kartei"]["allergien"] == "PPD"
    # Auskunft enthält die Kartei, Anonymisierung leert sie
    assert c.get(f"/kunden/{kid}/auskunft", headers=h).json()["kunde"]["kartei"]["allergien"] == "PPD"
    with db.sitzungen() as s:
        from core import loeschung
        k = s.get(Kunde, kid)
        loeschung.anonymisieren(s, k, "mensch", "Test")
        s.commit()
    assert c.get(f"/kunden/{kid}", headers=h).json()["kunde"]["kartei"] == {}


def test_no_show_zaehlt_und_setzt_vorgang_zurueck(umgebung):
    c, db, ids, h = umgebung
    fall_id, termin_id = fall_mit_termin(c, h, ids, ids["monteur_gas"])
    assert c.post(f"/termine/{termin_id}/nicht-erschienen", headers=h).status_code == 409   # liegt noch in der Zukunft
    with db.sitzungen() as s:
        t = s.get(Termin, termin_id)
        t.beginn, t.ende = t.beginn - timedelta(days=1), t.ende - timedelta(days=1)
        s.commit()
    r = c.post(f"/termine/{termin_id}/nicht-erschienen", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "nicht_erschienen"
    assert c.post(f"/termine/{termin_id}/nicht-erschienen", headers=h).json()["status"] == "nicht_erschienen"   # idempotent
    fall = c.get(f"/servicefaelle/{fall_id}", headers=h).json()["servicefall"]
    assert fall["status"] == "qualifiziert"
    kunde = c.get(f"/kunden/{fall['kunde_id']}", headers=h).json()["kunde"]
    assert kunde["nicht_erschienen"] == 1


# --- Online-Buchung ------------------------------------------------------------------------------
def test_online_buchung_von_leistung_bis_termin(umgebung):
    c, db, ids, h = umgebung
    c.put("/betrieb/stammdaten", headers=h, json={**STAMMDATEN, "basis_url": "https://salon.example.test", "no_show_hinweis": "Bitte sagen Sie bis 24 h vorher ab."})
    friseur(c, h)
    assert c.get("/betrieb/buchung", headers=h).json() == {"aktiv": False, "token": None, "pfad": None, "link": None,
                                                          **{k: v for k, v in c.get("/betrieb/buchung", headers=h).json().items() if k not in ("aktiv", "token", "pfad", "link")}}
    b = c.put("/betrieb/buchung", headers=h).json()
    assert b["aktiv"] and b["link"].startswith("https://salon.example.test/buchung/#") and b["pfad"].startswith("/buchung/#")
    token = b["pfad"].split("#", 1)[1]
    assert c.get("/buchung/").status_code == 200 and "buchung.js" in c.get("/buchung/").text and c.get("/buchung/buchung.js").status_code == 200
    assert c.get(f"/buchung/{ids['mandant']}.falsch").status_code == 404
    angebot = c.get(f"/buchung/{token}").json()          # ohne Mandantenkopf, öffentlich
    assert angebot["betrieb"]["name"] == "Demo Haustechnik GmbH" and angebot["branche"]["adresse_noetig"] is False
    assert len(angebot["leistungen"]) == 9 and len(angebot["mitarbeiter"]) == 2
    herren = next(x for x in angebot["leistungen"] if x["nummer"] == "L-310")
    assert herren["preis_brutto"] == "32.00" and herren["dauer_minuten"] == 30
    faerben = next(x for x in angebot["leistungen"] if x["nummer"] == "L-330")
    assert c.get(f"/buchung/{token}/zeiten", params={"leistung_id": faerben["id"]}).json()["tage"] == []   # niemand hat Qualifikation farbe
    z = c.get(f"/buchung/{token}/zeiten", params={"leistung_id": herren["id"]}).json()
    assert z["leistung"]["dauer_minuten"] == 30 and len(z["tage"]) >= 5 and len(z["tage"][0]["zeiten"]) <= 6
    erste = z["tage"][0]["zeiten"][0]
    assert erste["uhrzeit"] >= "10:00"   # Mindestvorlauf 2 h ab 08:00
    nur_b = c.get(f"/buchung/{token}/zeiten", params={"leistung_id": herren["id"], "ressource_id": ids["monteur_kaelte"]}).json()
    assert {x["ressource_id"] for t in nur_b["tage"] for x in t["zeiten"]} == {ids["monteur_kaelte"]}
    daten = {"leistung_id": herren["id"], "ressource_id": erste["ressource_id"], "beginn": erste["beginn"], "name": "Jonas Neu", "telefon": "0221 99 88 77",
             "email": "jonas@example.test", "hinweis": "Bitte kurz", "einwilligung_email": True}
    assert c.post(f"/buchung/{token}", json={**daten, "website": "http://spam"}).json() == {"status": "angenommen"}   # Honeypot
    assert c.post(f"/buchung/{token}", json={**daten, "telefon": None, "email": None}).status_code == 422
    assert c.post(f"/buchung/{token}", json={**daten, "beginn": DEMO_ZEIT.isoformat()}).status_code == 409   # zu kurzfristig
    r = c.post(f"/buchung/{token}", json=daten)
    assert r.status_code == 201, r.text
    e = r.json()
    assert e["verbindlich"] is False and e["portal_link"].startswith("https://salon.example.test/portal/#") and "vorgemerkt" in e["hinweis"]
    fall = c.get(f"/servicefaelle/{e['servicefall_id']}", headers=h).json()["servicefall"]
    assert fall["quelle"] == "web" and fall["anliegen"] == "Schneiden Herren – Bitte kurz" and fall["dauer_minuten"] == 30
    kunde = c.get(f"/kunden/{fall['kunde_id']}", headers=h).json()["kunde"]
    assert kunde["name"] == "Jonas Neu" and kunde["einwilligungen"]["email"] is True
    # Zweite Buchung derselben Rufnummer legt keinen zweiten Kunden an
    zweite = z["tage"][1]["zeiten"][0]
    r2 = c.post(f"/buchung/{token}", json={**daten, "ressource_id": zweite["ressource_id"], "beginn": zweite["beginn"], "name": "J. Neu"})
    assert r2.status_code == 201 and c.get(f"/servicefaelle/{r2.json()['servicefall_id']}", headers=h).json()["servicefall"]["kunde_id"] == fall["kunde_id"]
    # Freigabe termin_bestaetigen (Klasse B) macht den Termin verbindlich; Terminbestätigung trägt Branchenhinweise
    f = next(x for x in c.get("/freigaben", headers=h).json() if x["aktion"] == "termin_bestaetigen" and x["servicefall_id"] == e["servicefall_id"])
    assert c.post(f"/freigaben/{f['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "buero"}).status_code == 200
    assert c.get(f"/servicefaelle/{e['servicefall_id']}", headers=h).json()["termine"][0]["status"] == "bestaetigt"
    gesendet = []
    with db.sitzungen() as s:
        abarbeiten(s, {"endkunde": EmailAdapter({"absender": "buero@example.test", "host": "x"}, versand=gesendet.append)}, DEMO_ZEIT)
    best = [n.get_content() for n in gesendet if n["Subject"].startswith("Terminbestätigung")]
    assert best and "gewaschenem Haar" in best[0] and "24 h vorher" in best[0] and "Monteur: Monteur" in best[0]
    # Abschalten macht den Link ungültig
    assert c.put("/betrieb/buchung", headers=h, params={"aktiv": "false"}).json()["aktiv"] is False
    assert c.get(f"/buchung/{token}").status_code == 404


def test_shk_buchung_verlangt_adresse_und_laeuft_ueber_wartungsleistung(umgebung):
    c, db, ids, h = umgebung
    assert c.put("/betrieb/branche", headers=h, json={"branche": "shk"}).json()["leistungen_angelegt"] > 0
    token = c.put("/betrieb/buchung", headers=h).json()["pfad"].split("#", 1)[1]
    a = c.get(f"/buchung/{token}").json()
    assert a["branche"]["adresse_noetig"] is True
    wartung = next(x for x in a["leistungen"] if x["nummer"] == "L-100")
    z = c.get(f"/buchung/{token}/zeiten", params={"leistung_id": wartung["id"]}).json()
    assert {x["ressource_id"] for t in z["tage"] for x in t["zeiten"]} == {ids["monteur_gas"]}   # Qualifikation gas


# --- Gutscheine ----------------------------------------------------------------------------------
def test_gutschein_ausstellen_einloesen_storno_und_pdf(umgebung):
    c, db, ids, h = umgebung
    c.put("/betrieb/stammdaten", headers=h, json=STAMMDATEN)
    g = c.post("/gutscheine", headers=h, json={"wert": 50, "kunde_id": ids["kunde"], "empfaenger": "Oma Erna"}).json()
    assert len(g["code"]) == 14 and g["restwert"] == "50.00" and g["gueltig_bis"] == f"{DEMO_ZEIT.year + 3}-12-31" and g["status"] == "aktiv"
    assert c.get(f"/gutscheine/{g['code'].lower()}", headers=h).json()["empfaenger"] == "Oma Erna"   # Code ist unempfindlich gegen Schreibweise
    assert c.get("/gutscheine/XXXX-0000-YYYY", headers=h).status_code == 404
    assert c.post(f"/gutscheine/{g['code']}/einloesen", headers=h, json={"betrag": 20}).json()["restwert"] == "30.00"
    assert c.post(f"/gutscheine/{g['code']}/storno", headers=h).status_code == 409                       # schon teilweise eingelöst
    e = c.post(f"/gutscheine/{g['code']}/einloesen", headers=h, json={"betrag": 40}).json()              # Rechnungsbetrag > Rest: nur der Rest wird eingelöst
    assert e["eingeloest"] == "30.00" and e["restwert"] == "0.00" and e["status"] == "eingeloest"
    assert c.post(f"/gutscheine/{g['code']}/einloesen", headers=h, json={"betrag": 1}).status_code == 409
    pdf = c.get(f"/gutscheine/{g['code']}/pdf", headers=h)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    g2 = c.post("/gutscheine", headers=h, json={"wert": 25}).json()
    assert c.post(f"/gutscheine/{g2['code']}/storno", headers=h, params={"grund": "Fehleingabe"}).json()["status"] == "storniert"
    g3 = c.post("/gutscheine", headers=h, json={"wert": 10}).json()
    with db.sitzungen() as s:
        s.get(Gutschein, g3["id"]).gueltig_bis = date(2025, 12, 31)
        s.commit()
    assert c.post(f"/gutscheine/{g3['code']}/einloesen", headers=h, json={"betrag": 5}).status_code == 409   # abgelaufen
    u = c.get("/gutscheine", headers=h).json()
    assert u["offen_summe"] == "0.00" and len(u["gutscheine"]) == 3 and {x["status"] for x in u["gutscheine"]} == {"eingeloest", "storniert", "abgelaufen"}
    assert c.post("/gutscheine", headers=h, json={"wert": 0}).status_code == 422


def test_gutschein_hilfsfunktionen():
    code = gutscheines_code = gutscheine.code_erzeugen()
    assert len(code) == 14 and all(ch not in gutscheines_code for ch in "0O1I")


# --- Wiederkehr ----------------------------------------------------------------------------------
def test_wiederkehr_erinnerung_im_tageslauf(umgebung):
    c, db, ids, h = umgebung
    c.put("/betrieb/stammdaten", headers=h, json={**STAMMDATEN, "basis_url": "https://salon.example.test"})
    friseur(c, h)
    c.put("/betrieb/buchung", headers=h)
    assert c.post("/tagesaufgaben", headers=h).json()["wiederkehr"] == 0
    k = c.post("/kunden", headers=h, json={"name": "Lena Stamm", "telefon": "0221 555 001", "email": "lena@example.test"}).json()
    c.put(f"/kunden/{k['id']}/einwilligungen", headers=h, json={"email": True})
    with db.sitzungen() as s:
        f = Servicefall(mandant_id=ids["mandant"], kunde_id=k["id"], anliegen="Schneiden Damen", status="abgeschlossen", quelle="web")
        s.add(f); s.flush()
        beginn = DEMO_ZEIT - timedelta(days=42)
        s.add(Termin(mandant_id=ids["mandant"], servicefall_id=f.id, ressource_id=ids["monteur_gas"], beginn=beginn, ende=beginn + timedelta(hours=1), status="bestaetigt"))
        s.commit()
    assert c.post("/tagesaufgaben", headers=h).json()["wiederkehr"] == 1
    assert c.post("/tagesaufgaben", headers=h).json()["wiederkehr"] == 0   # idempotent am selben Tag
    with db.sitzungen() as s:
        fr = s.scalar(select(Freigabe).where(Freigabe.aktion == "nachricht_an_endkunde"))
        assert fr is not None and fr.payload["kunde_id"] == k["id"] and "/buchung/#" in fr.payload["text"] and "42 Tage" in fr.payload["text"]
    # Ohne Einwilligung keine Erinnerung
    c.put(f"/kunden/{k['id']}/einwilligungen", headers=h, json={"email": False})
    with db.sitzungen() as s:
        for ev in s.scalars(select(Ereignis).where(Ereignis.typ == "wiederkehr.erinnert")).all():
            s.delete(ev)
        s.commit()
    assert c.post("/tagesaufgaben", headers=h).json()["wiederkehr"] == 0


def test_branchen_hilfsfunktionen():
    assert len(branchen.BRANCHEN) == 17 and sum(len(b.leistungen) for b in branchen.BRANCHEN.values()) >= 100
    nummern = [le.nummer for b in branchen.BRANCHEN.values() for le in b.leistungen]
    assert len(nummern) == len(set(nummern))
    for b in branchen.BRANCHEN.values():
        assert set(b.begriffe) >= {"ressource", "ressourcen", "einsatz", "vorgang", "kartei", "anlagen"}
        assert b.gesundheitsdaten == any(f.sensibel for f in b.kartei)
