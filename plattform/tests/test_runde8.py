"""Runde 8: Betriebswebsite je Mandant (SSR, SEO, Kontaktformular, eigene Domain, veröffentlichte Bewertungen) und Upsell
(Empfehlungen aus Lebenszyklus, Risiko, Wartungsvertrag, Förderung, Branchenpaketen; Angebot/Vertrag per Klick; Portal; Kampagne)."""
from datetime import timedelta

from sqlalchemy import select

from core.demo import DEMO_ZEIT
from core.modelle import Angebot, Ereignis, Servicefall, Vertrag
from tests.test_einsatz import fall_mit_termin

HEUTE = DEMO_ZEIT.date()


def erledigt(db, fall_id):
    with db.sitzungen() as s:
        s.get(Servicefall, fall_id).status = "durchgefuehrt"
        s.commit()


def bewertung(c, h, db, ids, sterne, text, stunde=9):
    fall = c.post("/voice/servicefall", headers=h, json={"telefon": "0221 1234567", "name": "Familie Beispiel", "anliegen": "Pumpe defekt"}).json()
    fall_id = fall["servicefall_id"]
    r = c.post(f"/servicefaelle/{fall_id}/termine", headers=h, json={"ressource_id": ids["monteur_gas"], "beginn": DEMO_ZEIT.replace(hour=stunde).isoformat()})
    assert r.status_code == 201, r.text
    for f in c.get("/freigaben", headers=h).json():
        c.post(f"/freigaben/{f['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "buero"})
    erledigt(db, fall_id)
    token = c.get(f"/servicefaelle/{fall_id}/portal", headers=h).json()["pfad"].rsplit("/", 1)[-1]
    assert c.post(f"/portal/faelle/{fall_id}/bewertung?token={token}", json={"sterne": sterne, "text": text}).status_code == 200
    return fall_id, token


# --- Website -----------------------------------------------------------------------------------------------
def test_website_wird_aus_stammdaten_gerendert_mit_seo_und_rechtstexten(umgebung):
    c, db, ids, h = umgebung
    c.put("/betrieb/stammdaten", headers=h, json={"strasse": "Werkstattstraße 12", "plz": "50823", "ort": "Köln", "telefon": "0221 123456", "email": "info@beispiel.de",
                                                   "inhaber": "Erika Beispiel", "ust_id": "DE123456789", "basis_url": "https://backoffice.example.test"})
    st = c.get("/betrieb/website", headers=h).json()
    assert st["konfig"]["slug"] == "demo-haustechnik-gmbh" and not st["konfig"]["aktiv"] and st["fehlend"] == ["Claim", "Über uns"]
    assert c.get(f"/web/{st['konfig']['slug']}").status_code == 404                      # nicht veröffentlicht
    r = c.get(f"/web/{st['konfig']['slug']}?vorschau=1")
    assert r.status_code == 200 and "Vorschau" in r.text and 'name="robots" content="noindex"' in r.text
    assert c.put("/betrieb/website", headers=h, json={"slug": "Demo Haus!"}).json()["konfig"]["slug"] == "demo-haus"
    assert c.put("/betrieb/website", headers=h, json={"farbe": "blau"}).status_code == 422
    assert c.put("/betrieb/website", headers=h, json={"domain": "nicht gültig"}).status_code == 422
    st = c.put("/betrieb/website", headers=h, json={"aktiv": True, "slug": "demo-haustechnik", "claim": "Heizung, Bad und Notdienst in Köln", "ueber_uns": "Seit 1998 in Ehrenfeld.\nMeisterbetrieb.",
                                                     "farbe": "#B4541A", "bereiche": {"preise": True, "team": True}, "einzugsgebiet": "Köln, Pulheim, Frechen", "notdienst_telefon": "0221 999"}).json()
    assert st["konfig"]["aktiv"] and st["konfig"]["farbe"] == "#b4541a" and st["konfig"]["bereiche"]["preise"] and st["konfig"]["bereiche"]["buchung"]
    assert st["url"] == "https://backoffice.example.test/web/demo-haustechnik" and st["fehlend"] == []
    r = c.get("/web/demo-haustechnik")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and "max-age=300" in r.headers["cache-control"]
    t = r.text
    assert "<title>Demo Haustechnik GmbH – Sanitär, Heizung, Klima Köln</title>" in t and "Heizung, Bad und Notdienst in Köln" in t
    assert '"@type": "LocalBusiness"' in t and '"telephone": "0221 123456"' in t and "--akzent: #b4541a" in t and "Vorschau" not in t
    assert "Notdienst anrufen: 0221 999" in t and "Seit 1998 in Ehrenfeld." in t and "Monteur A" in t and "Köln, Pulheim, Frechen" in t
    assert 'href="/buchung/#' not in t                       # Buchung erst nach Aktivierung
    assert "<script" not in t.replace('<script type="application/ld+json">', "")   # kein JavaScript (CSP script-src 'self')
    assert "Wartung Gasheizung" in t or "Leistungen" in t
    c.put("/betrieb/buchung?aktiv=true", headers=h)
    t = c.get("/web/demo-haustechnik").text
    assert 'href="/buchung/#' in t and "Online Termin buchen" in t
    imp = c.get("/web/demo-haustechnik/impressum").text
    assert "Erika Beispiel" in imp and "DE123456789" in imp and "§ 5 DDG" in imp and "§ 36 VSBG" in imp
    ds = c.get("/web/demo-haustechnik/datenschutz").text
    assert "Art. 13 DSGVO" in ds and "Telefonassistent" in ds and "info@beispiel.de" in ds
    sm = c.get("/web/demo-haustechnik/sitemap.xml")
    assert sm.status_code == 200 and "<loc>https://backoffice.example.test/web/demo-haustechnik/impressum</loc>" in sm.text
    assert "Allow: /" in c.get("/web/demo-haustechnik/robots.txt").text
    assert c.get("/web/unbekannt").status_code == 404
    with db.sitzungen() as s:
        assert s.scalar(select(Ereignis).where(Ereignis.typ == "website.geaendert").order_by(Ereignis.id.desc())).payload["slug"] == "demo-haustechnik"


def test_website_kontaktformular_eigene_domain_und_bewertungen(umgebung):
    c, db, ids, h = umgebung
    c.put("/betrieb/stammdaten", headers=h, json={"strasse": "Werkstattstraße 12", "plz": "50823", "ort": "Köln", "telefon": "0221 123456", "email": "info@beispiel.de", "inhaber": "Erika Beispiel"})
    c.put("/betrieb/website", headers=h, json={"aktiv": True, "slug": "beispiel", "domain": "https://www.beispiel-haustechnik.de/"})
    st = c.get("/betrieb/website", headers=h).json()
    assert st["konfig"]["domain"] == "www.beispiel-haustechnik.de" and st["url"] == "https://www.beispiel-haustechnik.de"
    # Caddy on-demand TLS und Host-Umschreibung
    assert c.get("/web/domain-pruefen?domain=www.beispiel-haustechnik.de").status_code == 200
    assert c.get("/web/domain-pruefen?domain=beispiel-haustechnik.de").status_code == 200
    assert c.get("/web/domain-pruefen?domain=fremd.de").status_code == 404
    r = c.get("/", headers={"host": "www.beispiel-haustechnik.de"})
    assert r.status_code == 200 and "Demo Haustechnik" in r.text and 'action="/anfrage"' in r.text and 'href="/impressum"' in r.text
    assert c.get("/impressum", headers={"host": "beispiel-haustechnik.de"}).status_code == 200
    assert "https://www.beispiel-haustechnik.de/impressum" in c.get("/sitemap.xml", headers={"host": "www.beispiel-haustechnik.de"}).text
    assert c.get("/", headers={"host": "localhost"}).status_code in (200, 307)   # Plattform selbst bleibt
    # Kontaktformular: Honeypot, Validierung, Anfrage → Kunde, Vorgang, Kommunikation
    assert c.post("/web/beispiel/anfrage", data={"name": "Spam", "nachricht": "x" * 20, "website": "http://spam"}).status_code in (200, 303)
    r = c.post("/web/beispiel/anfrage", data={"name": "Jonas Weber", "nachricht": "Heizung macht Geräusche", "telefon": ""}, headers={"accept": "application/json"})
    assert r.status_code == 422 and "Telefon oder E-Mail" in r.json()["detail"]
    r = c.post("/web/beispiel/anfrage", data={"name": "Jonas Weber", "telefon": "0221 445566", "email": "jonas@example.de", "adresse": "Am Hang 2, 50939 Köln", "nachricht": "Heizung macht Geräusche"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/web/beispiel?gesendet=1#kontakt"
    assert "Ihre Anfrage ist eingegangen" in c.get("/web/beispiel?gesendet=1").text
    anfragen = c.get("/anfragen", headers=h).json()
    a = next(x for x in anfragen if x["name"] == "Jonas Weber")
    assert a["kanal"] == "web" and a["status"] == "uebernommen" and a["servicefall_id"]
    k = c.get(f"/kunden/{a['kunde_id']}", headers=h).json()["kunde"]
    assert k["strasse"] == "Am Hang 2" and k["plz"] == "50939" and k["ort"] == "Köln" and k["telefon"] == "+49221445566"
    # gleiche Anfrage in derselben Stunde ist idempotent
    c.post("/web/beispiel/anfrage", data={"name": "Jonas Weber", "telefon": "0221 445566", "email": "jonas@example.de", "adresse": "Am Hang 2, 50939 Köln", "nachricht": "Heizung macht Geräusche"}, follow_redirects=False)
    assert len([x for x in c.get("/anfragen", headers=h).json() if x["name"] == "Jonas Weber"]) == 1
    # Bewertungen: Durchschnitt sofort, Texte erst nach Veröffentlichung und nur ab Mindeststernen
    bewertung(c, h, db, ids, 5, "Schnell, sauber, freundlich.")
    bewertung(c, h, db, ids, 2, "Zu spät gekommen.", stunde=13)
    t = c.get("/web/beispiel").text
    assert "Kundenstimmen" in t and "3.5 von 5 aus 2 Bewertungen" in t and "Schnell, sauber" not in t
    assert '"aggregateRating": {"@type": "AggregateRating", "ratingValue": 3.5, "reviewCount": 2' in t
    texte = c.get("/betrieb/website", headers=h).json()["bewertungen"]["texte"]
    assert len(texte) == 2 and all(not x["veroeffentlicht"] for x in texte) and texte[0]["name"] == "Familie B."
    for x in texte:
        assert c.post(f"/bewertungen/{x['id']}/veroeffentlichen", headers=h, json={"veroeffentlicht": True}).json()["veroeffentlicht"]
    t = c.get("/web/beispiel").text
    assert "Schnell, sauber, freundlich." in t and "Zu spät gekommen." not in t    # 2 Sterne < Mindeststerne 4
    c.put("/betrieb/website", headers=h, json={"bewertungen_min_sterne": 1, "bereiche": {"bewertungen": True}})
    assert "Zu spät gekommen." in c.get("/web/beispiel").text
    assert c.post("/bewertungen/999999/veroeffentlichen", headers=h, json={}).status_code == 422
    # zweiter Betrieb kann Slug und Domain nicht doppelt nehmen
    m2 = c.post("/mandanten", json={"name": "Anderer Betrieb"}).json()
    h2 = {"X-Mandant": m2["id"]}
    assert c.put("/betrieb/website", headers=h2, json={"slug": "beispiel"}).status_code == 422
    assert c.put("/betrieb/website", headers=h2, json={"domain": "beispiel-haustechnik.de"}).status_code == 422
    assert c.put("/betrieb/website", headers=h2, json={"slug": "anderer"}).status_code == 200


# --- Upsell --------------------------------------------------------------------------------------------------
def test_empfehlungen_aus_lebenszyklus_vertrag_foerderung_und_paketen(umgebung):
    c, db, ids, h = umgebung
    assert c.patch(f"/anlagen/{ids['anlage']}", headers=h, json={"baujahr": 2004}).status_code == 200   # > 20 Jahre → Förderung
    e = c.get(f"/kunden/{ids['kunde']}/empfehlungen", headers=h).json()
    arten = {x["art"] for x in e["empfehlungen"]}
    schluessel = {x["schluessel"] for x in e["empfehlungen"]}
    assert {"erneuerung", "vertrag", "foerderung", "paket"} <= arten and "wartungsvertrag" in schluessel and "paket:hydraulischer_abgleich" in schluessel
    pumpe = next(x for x in e["empfehlungen"] if x["schluessel"].startswith("erneuerung:") and "pumpe" in x["titel"].lower())
    assert pumpe["prioritaet"] == 3 and pumpe["preis_brutto"] == "450.00" and pumpe["positionen"][0]["einzelpreis_netto"] == "378.15" and "Überfällig" in pumpe["begruendung"]
    vertrag = next(x for x in e["empfehlungen"] if x["schluessel"] == "wartungsvertrag")
    assert vertrag["aktion"] == "vertrag" and float(vertrag["preis_brutto"]) > 100
    foerd = next(x for x in e["empfehlungen"] if x["art"] == "foerderung")
    assert "12500" in foerd["begruendung"].replace(".", "") and foerd["aktion"] == "beratung" and foerd["positionen"] == []
    assert e["empfehlungen"][0]["prioritaet"] == 3 and float(e["potenzial"]) > 1000
    # Frost-Check nur im Oktober/November: DEMO_ZEIT ist November
    assert "paket:frostschutz_check" in schluessel and "paket:klima_service" not in schluessel
    # Paket mit Stichwort im Anliegen eines Vorgangs
    f = c.post("/servicefaelle", headers=h, json={"kunde_id": ids["kunde"], "anliegen": "Beratung neues Bad mit Dusche"}).json()
    ef = c.get(f"/servicefaelle/{f['id']}/empfehlungen", headers=h).json()["empfehlungen"]
    bad = next(x for x in ef if x["schluessel"] == "paket:bad_barrierefrei")
    assert bad["prioritaet"] == 2 and bad["aktion"] == "beratung"
    # Angebot aus Empfehlung mit einem Klick; danach 180 Tage unterdrückt
    a = c.post(f"/kunden/{ids['kunde']}/empfehlungen/{pumpe['schluessel']}/angebot", headers=h, json={"versenden": True}).json()
    assert a["brutto"] == "450.00" and a["status"] == "entwurf" and a["versand_beantragt"]
    assert any(fr["aktion"] == "angebot_senden" for fr in c.get("/freigaben", headers=h).json())
    assert pumpe["schluessel"] not in {x["schluessel"] for x in c.get(f"/kunden/{ids['kunde']}/empfehlungen", headers=h).json()["empfehlungen"]}
    assert c.post(f"/kunden/{ids['kunde']}/empfehlungen/{pumpe['schluessel']}/angebot", headers=h, json={}).status_code == 409   # nicht mehr verfügbar
    assert c.post(f"/kunden/{ids['kunde']}/empfehlungen/foerderung:{foerd['anlage_id']}/angebot", headers=h, json={}).status_code == 409   # Beratung ohne Festpreis
    # Ablehnen unterdrückt
    assert c.post(f"/kunden/{ids['kunde']}/empfehlungen/paket:thermostate/ablehnen", headers=h, json={"grund": "hat schon"}).json()["unterdrueckt_tage"] == 180
    assert "paket:thermostate" not in {x["schluessel"] for x in c.get(f"/kunden/{ids['kunde']}/empfehlungen", headers=h).json()["empfehlungen"]}
    # Wartungsvertrag per Klick: Vertrag, Frist, Kunde markiert, Empfehlung verschwindet
    v = c.post(f"/kunden/{ids['kunde']}/empfehlungen/wartungsvertrag/vertrag", headers=h).json()
    assert v["vertrag_id"] and v["beginn"] == HEUTE.isoformat()
    with db.sitzungen() as s:
        vt = s.get(Vertrag, v["vertrag_id"])
        assert vt.status == "aktiv" and vt.anlagen_ids == [ids["anlage"]] and vt.preis_jahr_netto == vertrag["positionen"][0]["einzelpreis_netto"]
    assert "wartungsvertrag" not in {x["schluessel"] for x in c.get(f"/kunden/{ids['kunde']}/empfehlungen", headers=h).json()["empfehlungen"]}
    assert c.post(f"/kunden/{ids['kunde']}/empfehlungen/wartungsvertrag/vertrag", headers=h).status_code == 409
    # Übersicht und Sprachagent
    u = c.get("/upsell/uebersicht", headers=h).json()
    assert u["kunden_mit_empfehlung"] >= 1 and u["letzte_90_tage"]["angeboten"] == 2 and u["letzte_90_tage"]["angenommen"] == 1 and u["letzte_90_tage"]["je_art"] == {"erneuerung": 1, "vertrag": 1}
    assert float(u["potenzial"]) > 0 and u["kunden"][0]["top"]
    vk = c.post("/voice/kunde-suchen", headers=h, json={"telefon": "0221 1234567"}).json()
    assert vk["wartungsvertrag"] and len(vk["empfehlungen"]) == 2 and all(x["titel"] for x in vk["empfehlungen"])
    assert len(c.get("/upsell/pakete", headers=h).json()) == 7


def test_upsell_pakete_je_branche_und_wiederkehr(umgebung):
    c, db, ids, h = umgebung
    c.post("/betrieb/einrichten", headers=h, json={"branche": "kfz", "name": "Autohaus", "sachmittel": [{"name": "Bühne 1", "typ": "hebebuehne"}]})
    o = c.post(f"/kunden/{ids['kunde']}/objekte", headers=h, json={"bezeichnung": "Fahrzeuge"}).json()
    c.post(f"/objekte/{o['id']}/anlagen", headers=h, json={"anlagentyp": "fahrzeug", "hersteller": "VW", "merkmale": {"kennzeichen": "K-MM 1", "hu_bis": (HEUTE + timedelta(days=40)).isoformat()}})
    e = c.get(f"/kunden/{ids['kunde']}/empfehlungen", headers=h).json()["empfehlungen"]
    schluessel = {x["schluessel"] for x in e}
    assert "paket:reifen_einlagerung" in schluessel and "paket:batterie_check" in schluessel and "paket:klimaservice" not in schluessel   # November
    assert any(x["schluessel"].startswith("erneuerung:") and "Hauptuntersuchung" in x["titel"] for x in e)
    f = c.post("/servicefaelle", headers=h, json={"kunde_id": ids["kunde"], "anliegen": "Klima kühlt nicht"}).json()
    ef = {x["schluessel"]: x for x in c.get(f"/servicefaelle/{f['id']}/empfehlungen", headers=h).json()["empfehlungen"]}
    assert "paket:klimaservice" in ef and ef["paket:klimaservice"]["prioritaet"] == 2
    assert len(c.get("/upsell/pakete", headers=h).json()) == 6
    # Salon: Wiederkehr-Empfehlung nach Ablauf des Intervalls, keine Anlagen/Verträge
    c.put("/betrieb/branche", headers=h, json={"branche": "friseur"})
    with db.sitzungen() as s:
        from core.modelle import Termin
        fall = Servicefall(mandant_id=ids["mandant"], kunde_id=ids["kunde"], anliegen="Schnitt", status="durchgefuehrt")
        s.add(fall)
        s.flush()
        s.add(Termin(mandant_id=ids["mandant"], servicefall_id=fall.id, ressource_id=ids["monteur_gas"], beginn=DEMO_ZEIT - timedelta(days=70), ende=DEMO_ZEIT - timedelta(days=70, hours=-1), status="bestaetigt"))
        s.commit()
    e = c.get(f"/kunden/{ids['kunde']}/empfehlungen", headers=h).json()["empfehlungen"]
    assert any(x["schluessel"] == "folgetermin" and x["aktion"] == "buchung" and "70 Tagen" in x["begruendung"] for x in e)
    assert not any(x["art"] == "vertrag" for x in e)


def test_portal_empfehlungen_anfordern_und_angebot_annehmen(umgebung):
    c, db, ids, h = umgebung
    fall_id, _ = fall_mit_termin(c, h, ids, ids["monteur_gas"])
    token = c.get(f"/servicefaelle/{fall_id}/portal", headers=h).json()["pfad"].rsplit("/", 1)[-1]
    d = c.get(f"/portal/faelle/{fall_id}?token={token}").json()
    assert 1 <= len(d["empfehlungen"]) <= 3 and all(x["art"] != "risiko" for x in d["empfehlungen"]) and d["empfehlungen"][0]["preis_brutto"]
    wahl = next(x for x in d["empfehlungen"] if x["aktion"] == "angebot")
    r = c.post(f"/portal/faelle/{fall_id}/empfehlungen/{wahl['schluessel']}/anfordern?token={token}").json()
    assert r["angebot_id"] and "Angebot" in r["hinweis"]
    assert c.post(f"/portal/faelle/{fall_id}/empfehlungen/{wahl['schluessel']}/anfordern?token=falsch").status_code == 404
    with db.sitzungen() as s:
        a = s.get(Angebot, r["angebot_id"])
        assert a.status == "entwurf" and a.servicefall_id == fall_id and a.titel == wahl["titel"]
        assert s.scalar(select(Ereignis).where(Ereignis.typ == "upsell.angefordert")).payload["schluessel"] == wahl["schluessel"]
    assert wahl["schluessel"] not in {x["schluessel"] for x in c.get(f"/portal/faelle/{fall_id}?token={token}").json()["empfehlungen"]}
    # Angebot im Portal nur annehmbar, wenn versendet
    assert c.post(f"/portal/faelle/{fall_id}/angebote/{r['angebot_id']}/annehmen?token={token}").status_code == 404
    fr = c.post(f"/angebote/{r['angebot_id']}/senden", headers=h).json()
    c.post(f"/freigaben/{fr['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "buero"})
    d = c.get(f"/portal/faelle/{fall_id}?token={token}").json()
    assert d["angebote"][0]["id"] == r["angebot_id"] and d["angebote"][0]["status"] == "versendet"
    an = c.post(f"/portal/faelle/{fall_id}/angebote/{r['angebot_id']}/annehmen?token={token}").json()
    assert an["angebot"]["status"] == "angenommen" and an["servicefall_id"] == fall_id and "Auftrag" in an["hinweis"]
    assert c.get(f"/portal/faelle/{fall_id}?token={token}").json()["angebote"][0]["status"] == "angenommen"
    u = c.get("/upsell/uebersicht", headers=h).json()["letzte_90_tage"]
    assert u["angeboten"] == 1 and u["angenommen"] == 1 and u["quote"] == 1.0 and u["umsatz_angenommen"] == a.brutto


def test_upsell_kampagne_im_tageslauf_nur_mit_einwilligung_und_einmalig(umgebung):
    c, db, ids, h = umgebung
    from core.tagesaufgaben import tagesaufgaben
    with db.sitzungen() as s:
        from core.modelle import Mandant
        m = s.get(Mandant, ids["mandant"])
        assert tagesaufgaben(s, m, DEMO_ZEIT)["upsell_kampagne"] == 0        # ohne Einwilligung keine Werbung (§ 7 UWG)
        s.commit()
    c.put(f"/kunden/{ids['kunde']}/einwilligungen", headers=h, json={"email": True})
    with db.sitzungen() as s:
        m = s.get(Mandant, ids["mandant"])
        assert tagesaufgaben(s, m, DEMO_ZEIT)["upsell_kampagne"] == 1
        s.commit()
        assert tagesaufgaben(s, m, DEMO_ZEIT + timedelta(days=1))["upsell_kampagne"] == 0   # einmal je 180 Tage
        s.commit()
    fr = [f for f in c.get("/freigaben", headers=h).json() if f["aktion"] == "nachricht_an_endkunde" and "Empfehlungen" in f["payload"]["betreff"]]
    assert len(fr) == 1 and "Umwälzpumpe" in fr[0]["payload"]["text"] or "Wartungsvertrag" in fr[0]["payload"]["text"]
