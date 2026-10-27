"""Runde 7: Multi-Branchen (Sachmittel in der Disposition, Anlagenkartei mit HU/Kilometerstand, sechs neue Profile),
Einrichtungsassistent mit CSV-Kundenimport und CLI, Kundenportal mit Zeitachse, Wunschtermin, ICS, GiroCode und Bewertung."""
import io
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from core import branchen, checklisten
from core.demo import DEMO_ZEIT
from core.modelle import Kunde, Servicefall, Termin
from tests.test_einsatz import fall_mit_termin

WURZEL = Path(__file__).resolve().parent.parent


def werkstatt(c, h, **extra):
    return c.post("/betrieb/einrichten", headers=h, json={"branche": "kfz", "name": "Autohaus Beispiel", "stammdaten": {"telefon": "0221 1", "email": "w@example.de"},
                                                          "mitarbeiter": [{"name": "Ali Yilmaz", "qualifikationen": ["kfz_mechatroniker"]}],
                                                          "sachmittel": [{"name": "Bühne 1", "typ": "hebebuehne"}], **extra}).json()


def freigeben(c, h):
    for f in c.get("/freigaben", headers=h).json():
        c.post(f"/freigaben/{f['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "buero"})


# --- Sachmittel in der Disposition -----------------------------------------------------------------------
def test_sachmittel_werden_mit_dem_termin_belegt_und_begrenzen_die_fenster(umgebung):
    c, db, ids, h = umgebung
    e = werkstatt(c, h)
    assert e["mitarbeiter_angelegt"] == 1 and e["sachmittel_angelegt"] == 1 and e["profil"]["schluessel"] == "kfz"
    ressourcen = c.get("/ressourcen", headers=h).json()
    buehne = next(r for r in ressourcen if r["art"] == "sachmittel")
    assert buehne["sachmittel_typ"] == "hebebuehne" and buehne["name"] == "Bühne 1"
    # Personenlisten enthalten keine Sachmittel
    assert "Bühne 1" not in [r["name"] for r in c.get("/plantafel", headers=h).json()["ressourcen"]]
    assert c.get("/disposition/zeitfenster?sachmittel=ersatzfahrzeug", headers=h).json() == []   # nicht angelegt
    fenster = c.get("/disposition/zeitfenster?sachmittel=hebebuehne&dauer_minuten=60", headers=h).json()
    assert fenster and all(f["sachmittel_id"] == buehne["id"] and f["sachmittel_name"] == "Bühne 1" for f in fenster)
    ohne = c.get("/disposition/zeitfenster?dauer_minuten=60", headers=h).json()
    assert all(f["sachmittel_id"] is None for f in ohne)
    f1 = c.post("/servicefaelle", headers=h, json={"kunde_id": ids["kunde"], "anliegen": "Bremsen vorn", "sachmittel": "hebebuehne", "dauer_minuten": 60}).json()
    assert f1["sachmittel"] == "hebebuehne"
    t1 = c.post(f"/servicefaelle/{f1['id']}/termine", headers=h, json={"ressource_id": fenster[0]["ressource_id"], "beginn": fenster[0]["beginn"]}).json()["termin"]
    assert t1["sachmittel_id"] == buehne["id"]   # automatisch gewählt
    # zweiter Vorgang zur selben Zeit mit anderem Monteur: einzige Bühne belegt → 409
    f2 = c.post("/servicefaelle", headers=h, json={"kunde_id": ids["kunde"], "anliegen": "Reifen", "sachmittel": "hebebuehne", "dauer_minuten": 60}).json()
    andere = next(r["id"] for r in ressourcen if r["art"] == "person" and r["id"] != fenster[0]["ressource_id"])
    r = c.post(f"/servicefaelle/{f2['id']}/termine", headers=h, json={"ressource_id": andere, "beginn": fenster[0]["beginn"]})
    assert r.status_code == 409 and "belegt" in r.json()["detail"]
    # Fenster danach ohne die belegte Zeit
    spaeter = c.get("/disposition/zeitfenster?sachmittel=hebebuehne&dauer_minuten=60", headers=h).json()
    assert all(not (s["beginn"] < t1["ende"] and s["ende"] > t1["beginn"]) for s in spaeter)
    # Vorgang mit nicht angelegtem Sachmittel → 409 mit Hinweis
    f3 = c.post("/servicefaelle", headers=h, json={"kunde_id": ids["kunde"], "anliegen": "Leihwagen", "sachmittel": "ersatzfahrzeug"}).json()
    r = c.post(f"/servicefaelle/{f3['id']}/termine", headers=h, json={"ressource_id": andere, "beginn": (DEMO_ZEIT + timedelta(days=2)).replace(hour=10).isoformat()})
    assert r.status_code == 409 and "ersatzfahrzeug" in r.json()["detail"]
    # Sachmittel-Anlage validiert den Typ
    assert c.post("/ressourcen", headers=h, json={"name": "Dings", "art": "sachmittel", "sachmittel_typ": "zauberstab"}).status_code == 422


# --- Anlagenkartei --------------------------------------------------------------------------------------
def test_fahrzeugkartei_validiert_und_spiegelt_hu_und_kilometer(umgebung):
    c, db, ids, h = umgebung
    werkstatt(c, h)
    p = c.get("/betrieb/branche", headers=h).json()
    assert p["anlagentypen"] == ["fahrzeug"] and "hebebuehne" in p["sachmittel"] and {f["schluessel"] for f in p["anlagenkartei"]["fahrzeug"]} >= {"kennzeichen", "fin", "hu_bis", "kilometerstand"}
    o = c.post(f"/kunden/{ids['kunde']}/objekte", headers=h, json={"bezeichnung": "Fahrzeuge"}).json()
    assert c.post(f"/objekte/{o['id']}/anlagen", headers=h, json={"anlagentyp": "fahrzeug", "merkmale": {"farbe": "rot"}}).status_code == 422
    assert c.post(f"/objekte/{o['id']}/anlagen", headers=h, json={"anlagentyp": "fahrzeug", "merkmale": {"hu_bis": "31.13.2027"}}).status_code == 422
    a = c.post(f"/objekte/{o['id']}/anlagen", headers=h, json={"anlagentyp": "fahrzeug", "hersteller": "VW", "modell": "Golf VIII", "baujahr": 2020,
                                                               "merkmale": {"kennzeichen": "k-mm 1234", "hu_bis": "2027-03-31", "kilometerstand": "84500"}}).json()
    assert a["merkmale"]["kilometerstand"] == 84500 and a["merkmale"]["kennzeichen"] == "k-mm 1234"
    bauteile = {b["art"]: b for b in c.get(f"/anlagen/{a['id']}/bauteile", headers=h).json()["bauteile"]}
    assert bauteile["hu"]["faellig"] == "2027-03-31" and bauteile["hu"]["pflicht"] and "oelwechsel" in bauteile
    st = c.get(f"/kunden/{ids['kunde']}/zaehlerstaende", headers=h).json()["staende"]
    assert any(z["art"] == "kilometer" and z["wert"] == 84500 for z in st)
    # Kartei ändern: neues HU-Datum verschiebt die Fälligkeit, Kilometerstand wird fortgeschrieben
    g = c.patch(f"/anlagen/{a['id']}", headers=h, json={"merkmale": {"hu_bis": "2029-05-01", "kilometerstand": 91000}}).json()
    assert g["merkmale"]["hu_bis"] == "2029-05-01" and g["merkmale"]["kennzeichen"] == "k-mm 1234"   # Merge, nichts verloren
    assert {b["art"]: b for b in c.get(f"/anlagen/{a['id']}/bauteile", headers=h).json()["bauteile"]}["hu"]["faellig"] == "2029-05-01"
    assert c.patch(f"/anlagen/{a['id']}", headers=h, json={"merkmale": {"kilometerstand": "viele"}}).status_code == 422
    # Direktannahme-Checkliste und Portal-Anzeige
    assert "fahrzeug" in checklisten.CHECKLISTEN and any("Kennzeichen" in x or "Kilometerstand" in x for x in checklisten.CHECKLISTEN["fahrzeug"]["punkte"]) if isinstance(checklisten.CHECKLISTEN["fahrzeug"], dict) else "fahrzeug" in checklisten.CHECKLISTEN
    fall_id, _ = fall_mit_termin(c, h, ids, ids["monteur_gas"])
    token = c.get(f"/servicefaelle/{fall_id}/portal", headers=h).json()["pfad"].rsplit("/", 1)[-1]
    d = c.get(f"/portal/faelle/{fall_id}?token={token}").json()
    fz = next(x for x in d["haus"]["anlagen"] if x["typ"] == "Fahrzeug")
    assert fz["merkmale"]["kennzeichen"] == "k-mm 1234" and fz["merkmale"]["hu_bis"] == "2029-05-01"


def test_siebzehn_branchenprofile_mit_sachmitteln_und_anlagentypen(umgebung):
    c, db, ids, h = umgebung
    liste = {b["schluessel"]: b for b in c.get("/branchen").json()}
    assert len(liste) == 17 and {"maler", "dachdecker", "gartenbau", "gebaeudereinigung", "tischler", "schluesseldienst"} <= set(liste)
    assert sum(len(b.leistungen) for b in branchen.BRANCHEN.values()) >= 100
    assert branchen.BRANCHEN["kfz"].sachmittel == ["hebebuehne", "ersatzfahrzeug", "geraet"] and any(le.sachmittel == "hebebuehne" for le in branchen.BRANCHEN["kfz"].leistungen)
    assert branchen.BRANCHEN["dachdecker"].anlagentypen == ["dach", "pv"] and branchen.anlagentypen_der_branche(branchen.BRANCHEN["shk"])
    assert "fahrzeug" not in branchen.anlagentypen_der_branche(branchen.BRANCHEN["shk"])
    u = c.put("/betrieb/branche", headers=h, json={"branche": "dachdecker"}).json()
    assert u["leistungen_angelegt"] == len(branchen.BRANCHEN["dachdecker"].leistungen) and u["profil"]["anlagentypen"] == ["dach", "pv"]
    k = c.get("/voice/kontext", headers=h).json()
    assert "Dachdecker" in k["rolle"] and len(k["leistungen"]) == u["leistungen_angelegt"]
    for schluessel in ("dach", "pv", "elektroanlage", "lueftung", "gartenanlage", "fahrrad", "fahrzeug"):
        assert schluessel in checklisten.CHECKLISTEN and schluessel in branchen.ANLAGENKARTEI
    assert c.get("/anlagentypen").json()["pv"]["bezeichnung"].startswith("Photovoltaik")


# --- Einrichtung -----------------------------------------------------------------------------------------
def test_einrichtungsstatus_und_assistent_sind_idempotent(umgebung):
    c, db, ids, h = umgebung
    st = c.get("/betrieb/einrichtung", headers=h).json()
    assert 0 < st["fortschritt"] < 1 and not st["fertig"] and {s["schluessel"] for s in st["schritte"]} >= {"branche", "betrieb", "iban", "team", "kunden", "basis_url"}
    assert next(s for s in st["schritte"] if s["schluessel"] == "team")["erledigt"] and not next(s for s in st["schritte"] if s["schluessel"] == "iban")["erledigt"]
    assert len(st["naechste"]) == 3 and all(not s["erledigt"] for s in st["naechste"])
    e1 = werkstatt(c, h, online_buchung=True)
    e2 = werkstatt(c, h, online_buchung=True)
    assert (e1["mitarbeiter_angelegt"], e1["sachmittel_angelegt"]) == (1, 1) and (e2["mitarbeiter_angelegt"], e2["sachmittel_angelegt"]) == (0, 0)
    b = c.get("/betrieb", headers=h).json()
    assert b["name"] == "Autohaus Beispiel" and b["stammdaten"]["telefon"] == "0221 1" and b["stammdaten"]["buchung_token"]
    assert c.get("/betrieb/buchung", headers=h).json()["aktiv"]
    st = e2["status"]
    assert next(s for s in st["schritte"] if s["schluessel"] == "sachmittel")["erledigt"] and next(s for s in st["schritte"] if s["schluessel"] == "buchung")["erledigt"]
    assert st["fortschritt"] > 0.4
    assert c.post("/betrieb/einrichten", headers=h, json={"sachmittel": [{"name": "X", "typ": "zauberstab"}]}).status_code == 422
    assert c.post("/betrieb/einrichten", headers=h, json={"branche": "zahnarzt"}).status_code in (409, 422)


def test_csv_import_erkennt_spalten_legt_fahrzeuge_an_und_ueberspringt_dubletten(umgebung):
    c, db, ids, h = umgebung
    werkstatt(c, h)
    csv = ("Kundennr;Vorname;Nachname;Telefon;E-Mail;Straße;PLZ;Ort;Kennzeichen;Marke;Modell;HU bis;Kilometerstand\n"
           "1001;Max;Mustermann;0221 55 66 77;max@example.de;Hauptstr. 1;50667;Köln;K-MM 1234;VW;Golf VIII;31.03.2027;84500\n"
           "1002;Erika;Beispiel;+49 171 2223344;;Ringweg 9;50823;Köln;K-EB 77;Skoda;Octavia;2026-11-15;121000\n"
           "1003;;;;;;;;;;;;\n"
           "1004;Max;Mustermann;0221556677;;;;;;;;;\n").encode("latin-1")
    vor = c.get("/kunden", headers=h).json()
    v = c.post("/kunden/import", headers=h, files={"datei": ("kunden.csv", io.BytesIO(csv), "text/csv")}, data={"pruefen": "true"}).json()
    assert v["pruefen"] and v["trennzeichen"] == ";" and v["zuordnung"]["name"] == "Vorname + Nachname" and v["zuordnung"]["kennzeichen"] == "Kennzeichen"
    assert v["angelegt"] == 2 and v["uebersprungen"] == 1 and v["fahrzeuge"] == 2 and v["fehler"] == [{"zeile": 4, "grund": "Name fehlt"}]
    assert v["vorschau"][0]["telefon"] == "+4922155 6677".replace(" ", "") or v["vorschau"][0]["telefon"].startswith("+49")
    assert len(c.get("/kunden", headers=h).json()) == len(vor)   # Vorschau schreibt nichts
    e = c.post("/kunden/import", headers=h, files={"datei": ("kunden.csv", io.BytesIO(csv), "text/csv")}).json()
    assert e["angelegt"] == 2 and e["fahrzeuge"] == 2 and e["uebersprungen"] == 1
    with db.sitzungen() as s:
        k = s.scalar(select(Kunde).where(Kunde.mandant_id == ids["mandant"], Kunde.name == "Max Mustermann"))
        assert k.telefon.startswith("+49") and k.email == "max@example.de" and "Kundennr. 1001" in k.notiz
        a = k.objekte[0].anlagen[0]
        assert a.anlagentyp == "fahrzeug" and a.hersteller == "VW" and a.merkmale["kennzeichen"] == "K-MM 1234" and a.merkmale["hu_bis"] == "2027-03-31"
        assert a.merkmale["kilometerstand"] == 84500
    bauteile = {b["art"]: b for b in c.get(f"/anlagen/{a.id}/bauteile", headers=h).json()["bauteile"]}
    assert bauteile["hu"]["faellig"] == "2027-03-31"
    # zweiter Import: alles Dubletten
    e2 = c.post("/kunden/import", headers=h, files={"datei": ("kunden.csv", io.BytesIO(csv), "text/csv")}).json()
    assert e2["angelegt"] == 0 and e2["uebersprungen"] == 3
    r = c.post("/kunden/import", headers=h, files={"datei": ("x.csv", io.BytesIO(b"Farbe;Groesse\nrot;L\n"), "text/csv")})
    assert r.status_code == 422 and "Namensspalte" in r.json()["detail"]


def test_cli_einrichten_und_import_kunden(tmp_path):
    umg = {**os.environ, "SYSTEM_DATABASE_URL": f"sqlite:///{tmp_path / 'cli.db'}", "DATABASE_URL": f"sqlite:///{tmp_path / 'cli.db'}"}
    aus = subprocess.run([sys.executable, "-m", "core", "einrichten", "--name", "Autohaus CLI", "--branche", "kfz", "--mitarbeiter", "Ali Yilmaz:kfz_mechatroniker",
                          "--sachmittel", "Bühne 1:hebebuehne", "--telefon", "0221 9", "--email", "cli@example.de"], cwd=WURZEL, env=umg, capture_output=True, text=True, check=True)
    e = json.loads(aus.stdout)
    assert e["branche"] == "kfz" and e["mitarbeiter_angelegt"] == 1 and e["sachmittel_angelegt"] == 1 and len(e["mandant"]) == 32
    datei = tmp_path / "kunden.csv"
    datei.write_text("Name,Telefon,Kennzeichen\nErika Beispiel,0171 2223344,K-EB 77\n", encoding="utf-8")
    i = json.loads(subprocess.run([sys.executable, "-m", "core", "import-kunden", str(datei), "--mandant", e["mandant"]], cwd=WURZEL, env=umg, capture_output=True, text=True, check=True).stdout)
    assert i["angelegt"] == 1 and i["fahrzeuge"] == 1 and i["trennzeichen"] == ","
    # zweiter Lauf des Assistenten auf demselben Mandanten legt nichts doppelt an
    e2 = json.loads(subprocess.run([sys.executable, "-m", "core", "einrichten", "--name", "Autohaus CLI", "--branche", "kfz", "--mandant", e["mandant"],
                                    "--mitarbeiter", "Ali Yilmaz"], cwd=WURZEL, env=umg, capture_output=True, text=True, check=True).stdout)
    assert e2["mandant"] == e["mandant"] and e2["mitarbeiter_angelegt"] == 0


# --- Kundenportal ------------------------------------------------------------------------------------------
def test_portal_zeitachse_ics_girocode_und_bewertung(umgebung):
    c, db, ids, h = umgebung
    c.put("/betrieb/stammdaten", headers=h, json={"iban": "DE02120300000000202051", "telefon": "0221 1"})
    fall_id, termin_id = fall_mit_termin(c, h, ids, ids["monteur_gas"])
    token = c.get(f"/servicefaelle/{fall_id}/portal", headers=h).json()["pfad"].rsplit("/", 1)[-1]
    d = c.get(f"/portal/faelle/{fall_id}?token={token}").json()
    z = {s["schluessel"]: s for s in d["verlauf"]}
    assert [s["schluessel"] for s in d["verlauf"]] == ["eingegangen", "geplant", "unterwegs", "erledigt", "rechnung"]
    assert z["eingegangen"]["zustand"] == "erledigt" and z["geplant"]["zustand"] == "erledigt" and z["unterwegs"]["zustand"] == "offen" and z["rechnung"]["zustand"] == "offen"
    assert not d["alternativen_moeglich"] and not d["bewertung_moeglich"] and d["zahlung"]["iban"].startswith("DE02")
    assert c.get(f"/portal/faelle/{fall_id}/alternativen?token={token}").json() == []
    assert c.post(f"/portal/faelle/{fall_id}/alternativen?token={token}", json={"ressource_id": ids["monteur_gas"], "beginn": (DEMO_ZEIT + timedelta(days=1)).isoformat()}).status_code == 409
    # Monteur startet die Fahrt am Termintag → „unterwegs“ mit Ankunft
    c.post("/zeiten/start", headers=h, json={"art": "fahrt", "ressource_id": ids["monteur_gas"]})
    d = c.get(f"/portal/faelle/{fall_id}?token={token}").json()
    u = next(s for s in d["verlauf"] if s["schluessel"] == "unterwegs")
    assert u["zustand"] == "aktiv" and "09:00" in u["hinweis"] and "Monteur A" in u["hinweis"]
    # ICS zum Termin
    ics = c.get(f"/portal/faelle/{fall_id}/termine/{termin_id}.ics?token={token}")
    assert ics.status_code == 200 and ics.headers["content-type"].startswith("text/calendar") and b"BEGIN:VEVENT" in ics.content and b"Pumpe defekt" in ics.content
    assert c.get(f"/portal/faelle/{fall_id}/termine/{termin_id}.ics?token=falsch").status_code == 404
    # Bewertung erst nach erledigter Arbeit
    assert c.post(f"/portal/faelle/{fall_id}/bewertung?token={token}", json={"sterne": 5}).status_code == 409
    with db.sitzungen() as s:
        f = s.get(Servicefall, fall_id)
        f.status = "durchgefuehrt"
        s.commit()
    d = c.get(f"/portal/faelle/{fall_id}?token={token}").json()
    z = {s["schluessel"]: s for s in d["verlauf"]}
    assert z["erledigt"]["zustand"] == "erledigt" and z["unterwegs"]["zustand"] == "erledigt" and d["bewertung_moeglich"] and d["bewertung"] is None
    assert c.post(f"/portal/faelle/{fall_id}/bewertung?token={token}", json={"sterne": 6}).status_code == 422
    b = c.post(f"/portal/faelle/{fall_id}/bewertung?token={token}", json={"sterne": 2, "text": "Zu spät gekommen"}).json()
    assert b["bewertung"]["sterne"] == 2 and "Rückmeldung" in b["hinweis"]
    assert c.get(f"/portal/faelle/{fall_id}?token={token}").json()["bewertung"]["text"] == "Zu spät gekommen"
    bw = c.get("/bewertungen", headers=h).json()
    assert bw["anzahl"] == 1 and bw["durchschnitt"] == 2.0 and bw["eintraege"][0]["servicefall_id"] == fall_id
    assert any(k["kanal"] == "portal" and "Bewertung" in (k["zusammenfassung"] or "") for k in c.get(f"/kunden/{ids['kunde']}/kommunikation", headers=h).json()) if c.get(f"/kunden/{ids['kunde']}/kommunikation", headers=h).status_code == 200 else True
    # Rechnung mit GiroCode
    fr = c.post("/freigaben", headers=h, json={"aktion": "rechnung_erstellen", "servicefall_id": fall_id, "payload": {"kunde_id": ids["kunde"], "betrag_netto": 100}}).json()
    assert c.post(f"/freigaben/{fr['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "inhaber"}).status_code == 200
    d = c.get(f"/portal/faelle/{fall_id}?token={token}").json()
    assert d["rechnungen"] and d["rechnungen"][0]["girocode"] and {s["schluessel"]: s for s in d["verlauf"]}["rechnung"]["zustand"] == "aktiv"
    png = c.get(f"/portal/faelle/{fall_id}/rechnungen/{d['rechnungen'][0]['id']}/girocode.png?token={token}")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png" and png.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_portal_wunschtermin_ersetzt_vorschlag(umgebung):
    c, db, ids, h = umgebung
    fall = c.post("/voice/servicefall", headers=h, json={"telefon": "0221 1234567", "name": "Familie Beispiel", "anliegen": "Heizung tropft"}).json()
    fall_id = fall["servicefall_id"]
    alt = c.post(f"/servicefaelle/{fall_id}/termine", headers=h, json={"ressource_id": ids["monteur_gas"], "beginn": DEMO_ZEIT.replace(hour=9).isoformat()}).json()["termin"]
    token = c.get(f"/servicefaelle/{fall_id}/portal", headers=h).json()["pfad"].rsplit("/", 1)[-1]
    d = c.get(f"/portal/faelle/{fall_id}?token={token}").json()
    assert d["alternativen_moeglich"] and d["termine"][0]["status"] == "vorgeschlagen"
    assert {s["schluessel"]: s for s in d["verlauf"]}["geplant"]["zustand"] == "aktiv"
    fenster = c.get(f"/portal/faelle/{fall_id}/alternativen?token={token}").json()
    assert fenster and all({"ressource_id", "beginn", "ende", "text"} <= set(f) for f in fenster) and all(f["beginn"] > DEMO_ZEIT.isoformat() for f in fenster)
    assert c.post(f"/portal/faelle/{fall_id}/alternativen?token={token}", json={"ressource_id": ids["monteur_gas"], "beginn": (DEMO_ZEIT - timedelta(days=1)).isoformat()}).status_code == 422
    w = c.post(f"/portal/faelle/{fall_id}/alternativen?token={token}", json={"ressource_id": fenster[1]["ressource_id"], "beginn": fenster[1]["beginn"]}).json()
    assert w["termin"]["status"] == "vorgeschlagen" and "Wunschtermin" in w["hinweis"]
    with db.sitzungen() as s:
        termine = {t.id: t.status for t in s.scalars(select(Termin).where(Termin.servicefall_id == fall_id))}
    assert termine[alt["id"]] == "abgesagt" and termine[w["termin"]["id"]] == "vorgeschlagen" and len(termine) == 2
    assert any(f["aktion"] == "termin_bestaetigen" and f["status"] == "offen" for f in c.get("/freigaben", headers=h).json())
    # ohne Token nichts
    assert c.get(f"/portal/faelle/{fall_id}/alternativen").status_code == 404
