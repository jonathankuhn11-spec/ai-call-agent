"""Monteur-Module: Zeitstempel, Rapport mit Unterschrift und PDF, Nachkalkulation, Bereitschaft (auch für den
Sprachagenten), Fristen mit Folgefrist, Anhänge und Lohnexport. Rollenprüfung über lokal signierte Tokens."""
import base64
import io
from datetime import timedelta

from PIL import Image
from sqlalchemy import select

from core.demo import DEMO_ZEIT
from core.modelle import Ereignis, Zeiteintrag
from tests.test_auth import token
from tests.test_auth import umgebung as oidc_umgebung  # noqa: F401
from tests.test_finanz import STAMMDATEN, rechnung_stellen


def png() -> str:
    puffer = io.BytesIO()
    Image.new("RGBA", (300, 100), (0, 0, 0, 0)).save(puffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(puffer.getvalue()).decode()


def fall_mit_termin(c, h, ids, ressource):
    fall = c.post("/voice/servicefall", headers=h, json={"telefon": "0221 1234567", "name": "Familie Beispiel", "anliegen": "Pumpe defekt"}).json()
    t = c.post(f"/servicefaelle/{fall['servicefall_id']}/termine", headers=h,
               json={"ressource_id": ressource, "beginn": DEMO_ZEIT.replace(hour=9).isoformat()}).json()
    for f in c.get("/freigaben", headers=h).json():
        c.post(f"/freigaben/{f['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "buero"})
    return fall["servicefall_id"], t["termin"]["id"]


def test_zeit_starten_wechseln_stoppen_und_nachtragen(umgebung):
    c, db, ids, h = umgebung
    fall_id, _ = fall_mit_termin(c, h, ids, ids["monteur_gas"])
    z1 = c.post("/zeiten/start", headers=h, json={"art": "fahrt", "ressource_id": ids["monteur_gas"]}).json()
    z2 = c.post("/zeiten/start", headers=h, json={"art": "arbeit", "ressource_id": ids["monteur_gas"], "servicefall_id": fall_id}).json()
    assert z1["ende"] is None and z2["ende"] is None
    laufend = c.get("/zeiten?laufend=true", headers=h).json()
    assert [z["id"] for z in laufend] == [z2["id"]]  # Start einer neuen Art beendet die laufende
    assert c.post(f"/zeiten/{z2['id']}/stop", headers=h).status_code == 200
    assert c.post(f"/zeiten/{z2['id']}/stop", headers=h).status_code == 422
    assert c.post("/zeiten", headers=h, json={"art": "arbeit", "ressource_id": ids["monteur_gas"], "beginn": DEMO_ZEIT.isoformat(),
                                               "ende": (DEMO_ZEIT - timedelta(hours=1)).isoformat()}).status_code == 422
    n = c.post("/zeiten", headers=h, json={"art": "arbeit", "ressource_id": ids["monteur_gas"], "servicefall_id": fall_id,
                                            "beginn": (DEMO_ZEIT - timedelta(hours=3)).isoformat(), "ende": (DEMO_ZEIT - timedelta(hours=1)).isoformat()}).json()
    assert n["minuten"] == 120 and n["quelle"] == "manuell"
    assert len(c.get(f"/zeiten?servicefall_id={fall_id}", headers=h).json()) == 2
    assert c.post("/zeiten/start", headers=h, json={"art": "urlaub", "ressource_id": ids["monteur_gas"]}).status_code == 422


def test_rapport_mit_unterschrift_pdf_und_statuswechsel(umgebung):
    c, db, ids, h = umgebung
    fall_id, termin_id = fall_mit_termin(c, h, ids, ids["monteur_gas"])
    assert c.get(f"/servicefaelle/{fall_id}", headers=h).json()["servicefall"]["status"] == "eingeplant"
    assert c.post(f"/servicefaelle/{fall_id}/rapport", headers=h, json={"taetigkeiten": "x", "unterschrift": "data:image/jpeg;base64,AAAA"}).status_code == 422
    r = c.post(f"/servicefaelle/{fall_id}/rapport", headers=h, json={
        "taetigkeiten": "Umwälzpumpe getauscht, Anlage entlüftet, Druck auf 1,6 bar eingestellt.", "arbeitszeit_minuten": 95, "fahrzeit_minuten": 30,
        "material": [{"menge": "1", "bezeichnung": "Umwälzpumpe UPM3"}], "messwerte": {"druck_bar": "1.6"}, "termin_id": termin_id,
        "ressource_id": ids["monteur_gas"], "unterschrift": png(), "unterzeichnet_von": "Frau Beispiel"}).json()
    assert r["nummer"] == "RA-2026-00001" and r["unterschrift_anhang_id"] and r["unterzeichnet_am"]
    assert c.get(f"/servicefaelle/{fall_id}", headers=h).json()["servicefall"]["status"] == "durchgefuehrt"
    pdf = c.get(f"/rapporte/{r['id']}/pdf", headers=h)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    with db.sitzungen() as s:
        # Rapportminuten werden zu Zeiteinträgen, wenn keine gestempelt wurden
        zeiten = s.scalars(select(Zeiteintrag).where(Zeiteintrag.servicefall_id == fall_id)).all()
        assert sorted(z.art for z in zeiten) == ["arbeit", "fahrt"] and all(z.quelle == "rapport" for z in zeiten)
        assert s.scalar(select(Ereignis).where(Ereignis.typ == "rapport.erstellt")) is not None
    assert c.get(f"/servicefaelle/{fall_id}/rapporte", headers=h).json()[0]["nummer"] == "RA-2026-00001"
    assert c.delete(f"/anhaenge/{r['unterschrift_anhang_id']}", headers=h).status_code == 409


def test_nachkalkulation_je_auftrag(umgebung):
    c, db, ids, h = umgebung
    c.put("/betrieb/stammdaten", headers=h, json={**STAMMDATEN, "stundensatz_kosten": 60})
    fall_id, _ = fall_mit_termin(c, h, ids, ids["monteur_gas"])
    c.post(f"/servicefaelle/{fall_id}/rapport", headers=h, json={"taetigkeiten": "Pumpe getauscht", "arbeitszeit_minuten": 120, "ressource_id": ids["monteur_gas"]})
    b = c.post("/belege", headers=h, data={"lieferant": "Großhandel", "netto": "100", "kategorie": "material", "servicefall_id": fall_id}).json()
    assert b["servicefall_id"] == fall_id
    rechnung_stellen(c, h, ids, betrag_netto=400, servicefall_id=fall_id)
    k = c.get(f"/servicefaelle/{fall_id}/kalkulation", headers=h).json()
    assert k["stunden"] == "2.00" and k["lohnkosten"] == "120.00" and k["material"] == "100.00" and k["umsatz_netto"] == "400.00"
    assert k["deckungsbeitrag"] == "180.00" and k["deckungsbeitrag_quote"] == "45.0" and k["soll_umsatz"] == "253.00"  # 2 h × 69 € + 100 € × 1,15
    assert k["vorschlag_positionen"][0]["menge"] == "2.00" and k["vorschlag_positionen"][1]["einzelpreis_netto"] == "115.00"


def test_bereitschaft_ueberschneidung_und_voice_notdienst(umgebung):
    c, db, ids, h = umgebung
    assert c.post("/voice/notdienst", headers=h).json()["bereitschaft"] is False
    von, bis = DEMO_ZEIT - timedelta(hours=1), DEMO_ZEIT + timedelta(days=2)
    b = c.post("/bereitschaft", headers=h, json={"ressource_id": ids["monteur_gas"], "von": von.isoformat(), "bis": bis.isoformat(), "telefon": "0171 1234567"}).json()
    assert b["telefon"] == "+491711234567"
    assert c.post("/bereitschaft", headers=h, json={"ressource_id": ids["monteur_kaelte"], "von": (von + timedelta(hours=5)).isoformat(),
                                                    "bis": (bis + timedelta(days=1)).isoformat(), "telefon": "0172 1"}).status_code == 422
    assert c.post("/bereitschaft", headers=h, json={"ressource_id": ids["monteur_kaelte"], "von": bis.isoformat(), "bis": von.isoformat(), "telefon": "1"}).status_code == 422
    n = c.post("/voice/notdienst", headers=h).json()
    assert n["bereitschaft"] is True and n["telefon"] == "+491711234567" and n["name"] == "Monteur A"
    assert c.get("/bereitschaft/aktuell", headers=h).json()["id"] == b["id"]
    assert c.get("/einsatz/heute?ressource_id=" + ids["monteur_gas"], headers=h).json()["bereitschaft"]["telefon"] == "+491711234567"
    assert c.delete(f"/bereitschaft/{b['id']}", headers=h).status_code == 204
    assert c.get("/bereitschaft/aktuell", headers=h).json() is None


def test_fristen_mit_folgefrist(umgebung):
    c, db, ids, h = umgebung
    heute = DEMO_ZEIT.date()
    f = c.post("/fristen", headers=h, json={"art": "pruefung", "bezeichnung": "UVV Transporter", "faellig": (heute + timedelta(days=20)).isoformat(),
                                             "intervall_monate": 12}).json()
    spaet = c.post("/fristen", headers=h, json={"art": "unterweisung", "bezeichnung": "Erste Hilfe", "faellig": (heute + timedelta(days=400)).isoformat()}).json()
    ueberfaellig = c.post("/fristen", headers=h, json={"art": "zertifikat", "bezeichnung": "Kälteschein", "faellig": (heute - timedelta(days=3)).isoformat(),
                                                       "bezug_typ": "ressource", "bezug_id": ids["monteur_kaelte"]}).json()
    assert c.post("/fristen", headers=h, json={"art": "zertifikat", "bezeichnung": "xx", "faellig": heute.isoformat(), "bezug_typ": "ressource", "bezug_id": "gibtsnicht"}).status_code == 404
    offen = [x["id"] for x in c.get("/fristen?vorlauf_tage=90", headers=h).json()]
    assert offen == [ueberfaellig["id"], f["id"]] and spaet["id"] not in offen
    e = c.post(f"/fristen/{f['id']}/erledigt", headers=h, json={"am": heute.isoformat()}).json()
    assert e["frist"]["erledigt_am"] == heute.isoformat() and e["folgefrist"]["faellig"] == (heute + timedelta(days=365)).isoformat()[:4] + e["folgefrist"]["faellig"][4:]
    assert e["folgefrist"]["faellig"][5:] == heute.isoformat()[5:]  # gleicher Tag, ein Jahr später
    assert c.post(f"/fristen/{f['id']}/erledigt", headers=h, json={}).status_code == 422
    e2 = c.post(f"/fristen/{ueberfaellig['id']}/erledigt", headers=h, json={}).json()
    assert e2["folgefrist"] is None


def test_anhaenge_foto_verkleinert_und_dokument_am_kunden(umgebung):
    c, db, ids, h = umgebung
    bild = io.BytesIO()
    Image.new("RGB", (3200, 2400), "blue").save(bild, "JPEG", quality=95)
    a = c.post("/anhaenge", headers=h, files={"datei": ("vorher.jpg", bild.getvalue(), "image/jpeg")},
               data={"bezug_typ": "kunde", "bezug_id": ids["kunde"], "art": "foto", "notiz": "Typenschild"}).json()
    assert a["mime"] == "image/jpeg" and a["groesse"] < len(bild.getvalue())
    inhalt = c.get(f"/anhaenge/{a['id']}/inhalt", headers=h)
    assert Image.open(io.BytesIO(inhalt.content)).size == (1600, 1200)
    assert c.post("/anhaenge", headers=h, files={"datei": ("x.bin", b"\0" * 11_000_000, "application/octet-stream")},
                  data={"bezug_typ": "kunde", "bezug_id": ids["kunde"]}).status_code == 422
    assert c.post("/anhaenge", headers=h, files={"datei": ("x.pdf", b"%PDF", "application/pdf")},
                  data={"bezug_typ": "kunde", "bezug_id": "fremd"}).status_code == 404
    liste = c.get(f"/anhaenge?bezug_typ=kunde&bezug_id={ids['kunde']}", headers=h).json()
    assert [x["notiz"] for x in liste] == ["Typenschild"]
    assert c.delete(f"/anhaenge/{a['id']}", headers=h).status_code == 204


def test_lohnexport_mit_nacht_und_sonntag(umgebung):
    c, db, ids, h = umgebung
    sonntag = DEMO_ZEIT.replace(hour=20, minute=0)
    while sonntag.weekday() != 6:
        sonntag += timedelta(days=1)
    c.post("/zeiten", headers=h, json={"art": "arbeit", "ressource_id": ids["monteur_gas"], "beginn": sonntag.isoformat(),
                                        "ende": (sonntag + timedelta(hours=4)).isoformat()})  # 20–24 UTC = 22–02 Berlin (Sommerzeit)
    monat = sonntag.strftime("%Y-%m")
    antwort = c.get(f"/zeiten/lohnexport?monat={monat}", headers=h)
    assert antwort.status_code == 200 and antwort.headers["content-type"].startswith("text/csv")
    zeilen = antwort.content.decode("utf-8-sig").splitlines()
    assert zeilen[0].startswith("Monteur;Anmeldename;Datum;Arbeit (min)")
    daten = [z.split(";") for z in zeilen[1:] if z.startswith("Monteur A")]
    assert daten and sum(int(d[3]) for d in daten) == 240
    assert sum(int(d[7]) for d in daten) > 0 and sum(int(d[8]) for d in daten) > 0  # Nacht- und Sonntagsminuten
    assert c.get("/zeiten/lohnexport?monat=2026-13", headers=h).status_code == 422


def test_monteur_bucht_nur_auf_sich_und_sieht_nur_eigene_faelle(oidc_umgebung):  # noqa: F811
    c, ids = oidc_umgebung
    inhaber, buero = token(ids["mandant"], ["inhaber"], name="chef"), token(ids["mandant"], ["buero"])
    monteur = token(ids["mandant"], ["monteur"], name="m.gas")
    c.patch(f"/ressourcen/{ids['monteur_gas']}", headers=inhaber, json={"benutzer": "m.gas"})
    eigener, termin = fall_mit_termin(c, buero, ids, ids["monteur_gas"])
    fremder, _ = fall_mit_termin(c, buero, ids, ids["monteur_kaelte"])
    heute = c.get("/einsatz/heute", headers=monteur).json()
    assert heute["ressource"]["id"] == ids["monteur_gas"] and [t["servicefall_id"] for t in heute["termine"]] == [eigener]
    assert c.post("/zeiten/start", headers=monteur, json={"art": "arbeit", "ressource_id": ids["monteur_kaelte"]}).status_code == 403
    assert c.post("/zeiten/start", headers=monteur, json={"art": "arbeit", "servicefall_id": fremder}).status_code in (403, 404)
    z = c.post("/zeiten/start", headers=monteur, json={"art": "arbeit", "servicefall_id": eigener}).json()
    assert z["ressource_id"] == ids["monteur_gas"]
    assert c.get("/einsatz/heute", headers=monteur).json()["laufend"]["id"] == z["id"]
    assert c.post(f"/servicefaelle/{fremder}/rapport", headers=monteur, json={"taetigkeiten": "xxx"}).status_code in (403, 404)
    r = c.post(f"/servicefaelle/{eigener}/rapport", headers=monteur, json={"taetigkeiten": "Pumpe getauscht", "termin_id": termin})
    assert r.status_code == 201 and r.json()["ressource_id"] == ids["monteur_gas"]
    # Belegfoto darf der Monteur hochladen, die Belegliste und Kalkulation sieht nur das Büro / der Inhaber
    assert c.post("/belege", headers=monteur, data={"lieferant": "Tanke", "brutto": "50", "kategorie": "fahrzeug"}).status_code == 201
    assert c.get("/belege", headers=monteur).status_code == 403
    assert c.get(f"/servicefaelle/{eigener}/kalkulation", headers=monteur).status_code == 403
    assert c.get(f"/servicefaelle/{eigener}/kalkulation", headers=buero).status_code == 200
    assert c.get("/zeiten/lohnexport?monat=2026-10", headers=buero).status_code == 403
    assert c.post("/bereitschaft", headers=monteur, json={"ressource_id": ids["monteur_gas"], "von": DEMO_ZEIT.isoformat(), "bis": (DEMO_ZEIT + timedelta(days=1)).isoformat(), "telefon": "1"}).status_code == 403
    assert {z["ressource_id"] for z in c.get("/zeiten", headers=monteur).json()} == {ids["monteur_gas"]}
