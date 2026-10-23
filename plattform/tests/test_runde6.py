"""Runde 6: Bauteil-Lebenszyklus und Erneuerungskalender, Dienstplan mit ArbZG-Prüfung und Vorschlag, Zeit- und
Urlaubskonto, Zählerstände mit Gradtags-Normierung, Gewährleistung/Garantie, Qualifikationsmatrix, Portal „Ihr Haus“."""
from datetime import date, datetime, timedelta

from sqlalchemy import select

from core import dienstplan, lebenszyklus, zaehler
from core.demo import DEMO_ZEIT
from core.modelle import Dienst, Ereignis, Ressource
from core.routing import Router
from tests.test_einsatz import fall_mit_termin

HEUTE = DEMO_ZEIT.date()                      # 2026-11-17, Dienstag
MONTAG = HEUTE - timedelta(days=HEUTE.weekday())


def freigeben(c, h):
    for f in c.get("/freigaben", headers=h).json():
        c.post(f"/freigaben/{f['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "buero"})


# --- Lebenszyklus ------------------------------------------------------------------------------------
def test_bauteile_entstehen_mit_der_anlage_und_ergeben_kalender_und_ruecklage(umgebung):
    c, db, ids, h = umgebung
    u = c.get(f"/anlagen/{ids['anlage']}/bauteile", headers=h).json()
    arten = {b["art"]: b for b in u["bauteile"]}
    assert {"waermeerzeuger", "umwaelzpumpe", "gas_dichtheit", "abgaswegepruefung"} <= set(arten)
    # Baujahr 2012 + 10 Jahre Pumpe → 2022 überfällig; Wärmeerzeuger 2012 + 18 = 2030 → bald/aktuell
    assert arten["umwaelzpumpe"]["status"] == "faellig" and arten["umwaelzpumpe"]["faellig"] == "2022-01-01"
    assert arten["waermeerzeuger"]["faellig"] == "2030-01-01" and arten["waermeerzeuger"]["status"] == "aktuell"
    # Prüfpflicht mit letzter Wartung 2025-09-10 + 24 Monate = 2027-09-10 → bald (≤ 365 Tage)
    assert arten["abgaswegepruefung"]["faellig"] == "2027-09-10" and arten["abgaswegepruefung"]["status"] == "bald"
    assert float(u["ruecklage_jahr"]) > 500   # 7500/18 + 450/10 + 350/12 + 900/15 + 180/5 + 1800/20 ≈ 683
    # idempotent
    assert c.post(f"/anlagen/{ids['anlage']}/bauteile/anlegen", headers=h).json()["angelegt"] == 0
    kal = c.get(f"/kunden/{ids['kunde']}/erneuerungskalender?jahre=5", headers=h).json()
    assert kal["anlagen"] == 1 and any(e["art"] == "umwaelzpumpe" for e in kal["ueberfaellig"])
    jahre = {j["jahr"]: j for j in kal["jahre"]}
    assert "waermeerzeuger" in {e["art"] for e in jahre[2030]["eintraege"]}
    assert float(jahre[2030]["kosten"]) >= 7500
    # Erneuerung verschiebt die Fälligkeit und protokolliert
    pumpe = arten["umwaelzpumpe"]
    e = c.post(f"/bauteile/{pumpe['id']}/erneuert", headers=h, json={"am": "2026-11-17", "kosten": "480.00"}).json()
    assert e["faellig"] == "2036-11-17" and e["status"] == "aktuell" and e["restnutzung_jahre"] == 10.0
    # Prüfung "geprüft" statt "erneuert"
    p = c.post(f"/bauteile/{arten['gas_dichtheit']['id']}/erneuert", headers=h, json={}).json()
    assert p["faellig"] == "2038-11-17"
    with db.sitzungen() as s:
        typen = [e.typ for e in s.scalars(select(Ereignis).where(Ereignis.entitaet_id == ids["anlage"]))]
    assert "bauteile.angelegt" in typen and "bauteil.erneuert" in typen and "bauteil.geprueft" in typen
    # eigenes Bauteil mit Validierung
    assert c.post(f"/anlagen/{ids['anlage']}/bauteile", headers=h, json={"art": "x", "bezeichnung": "Sonstiges"}).status_code == 422
    b = c.post(f"/anlagen/{ids['anlage']}/bauteile", headers=h, json={"art": "thermostat", "bezeichnung": "Raumthermostat", "nutzungsdauer_jahre": 12,
                                                                        "einbau": "2020-03-01", "kosten_schaetzung": "150.00"}).json()
    assert b["faellig"] == "2032-03-01"
    assert c.delete(f"/bauteile/{b['id']}", headers=h).status_code == 204
    faellig = c.get("/lebenszyklus/faellig?vorlauf_tage=400", headers=h).json()
    assert any(x["art"] == "abgaswegepruefung" and x["kunde"] == "Familie Beispiel" for x in faellig)
    assert "gas_brennwert" in c.get("/lebenszyklus/katalog", headers=h).json()


def test_lebenszyklus_erinnerung_nur_mit_einwilligung_und_idempotent(umgebung):
    c, db, ids, h = umgebung
    with db.sitzungen() as s:
        from core.modelle import Kunde, Mandant
        m = s.get(Mandant, ids["mandant"])
        k = s.get(Kunde, ids["kunde"])
        assert lebenszyklus.erinnerungen(s, m, HEUTE, vorlauf_tage=400) == 0     # keine Einwilligung
        k.einwilligungen = {"email": True}
        k.email = "beispiel@example.org"
        s.commit()
        assert lebenszyklus.erinnerungen(s, m, HEUTE, vorlauf_tage=400) == 1
        assert lebenszyklus.erinnerungen(s, m, HEUTE, vorlauf_tage=400) == 0     # nicht zweimal
        s.commit()
    offen = c.get("/freigaben", headers=h).json()
    assert any(f["aktion"] == "nachricht_an_endkunde" for f in offen)
    lauf = c.post("/tagesaufgaben", headers=h).json()
    assert {"erneuerungen", "qualifikationen_ablaufend", "verbrauch_auffaellig"} <= set(lauf)


# --- Dienstplan ---------------------------------------------------------------------------------------
def test_dienste_uebersteuern_regelarbeitszeit_in_der_disposition(umgebung):
    c, db, ids, h = umgebung
    c.app.state.router = Router(None)
    # Mittwoch frei, Donnerstag Spätschicht für Monteur A
    mi, do = MONTAG + timedelta(days=2), MONTAG + timedelta(days=3)
    assert c.post("/dienste", headers=h, json={"ressource_id": ids["monteur_gas"], "datum": mi.isoformat(), "art": "frei"}).status_code == 201
    d = c.post("/dienste", headers=h, json={"ressource_id": ids["monteur_gas"], "datum": do.isoformat(), "schicht": "spaet"}).json()
    assert d["beginn"] == "10:00" and d["ende"] == "18:30" and d["stunden"] == 8.0
    fenster = c.get(f"/disposition/zeitfenster?qualifikation=gas&tage=5&anzahl=20&ab={datetime.combine(mi, datetime.min.time()).isoformat()}", headers=h).json()
    tage = {f["beginn"][:10] for f in fenster}
    assert mi.isoformat() not in tage
    do_fenster = [f for f in fenster if f["beginn"][:10] == do.isoformat()]
    assert do_fenster and min(f["beginn"][11:16] for f in do_fenster) >= "10:00"
    # Woche zeigt Regel und Dienste; Zeitfenster-Tag 'frei' hat 0 h
    w = c.get(f"/dienstplan?von={MONTAG.isoformat()}", headers=h).json()
    zeile = next(z for z in w["zeilen"] if z["ressource_id"] == ids["monteur_gas"])
    assert zeile["tage"][2]["stunden"] == 0 and zeile["tage"][3]["dienste"][0]["schicht"] == "spaet" and zeile["tage"][0]["regel"] is True
    assert c.delete(f"/dienste/{d['id']}", headers=h).status_code == 204
    # Kapazität in der Prognose/Twin folgt den Diensten
    with db.sitzungen() as s:
        from core import prognose
        from core.modelle import Mandant
        m = s.get(Mandant, ids["mandant"])
        kap_mit_frei = prognose.kapazitaet(s, m, MONTAG)["brutto_h"]
        s.query(Dienst).delete()
        s.commit()
        assert prognose.kapazitaet(s, m, MONTAG)["brutto_h"] > kap_mit_frei


def test_arbeitszeitgesetz_pruefung_findet_verstoesse(umgebung):
    c, db, ids, h = umgebung
    r = ids["monteur_gas"]
    so = MONTAG + timedelta(days=6)
    # 11 h Tag mit 20 min Pause, danach Frühdienst mit nur 7,5 h Ruhezeit, Sonntagsarbeit
    c.post("/dienste", headers=h, json={"ressource_id": r, "datum": MONTAG.isoformat(), "beginn": "06:00", "ende": "17:20", "pause_minuten": 20})
    c.post("/dienste", headers=h, json={"ressource_id": r, "datum": (MONTAG + timedelta(days=1)).isoformat(), "beginn": "17:00", "ende": "22:00", "pause_minuten": 0})
    c.post("/dienste", headers=h, json={"ressource_id": r, "datum": (MONTAG + timedelta(days=2)).isoformat(), "schicht": "frueh"})
    c.post("/dienste", headers=h, json={"ressource_id": r, "datum": so.isoformat(), "schicht": "tag"})
    w = c.get(f"/dienstplan/pruefung?von={MONTAG.isoformat()}&tage=7", headers=h).json()
    arten = {x["art"] for x in w}
    assert {"tageshoechstarbeitszeit", "ruhepause", "ruhezeit", "sonntagsarbeit"} <= arten
    ruhe = next(x for x in w if x["art"] == "ruhezeit")
    assert ruhe["datum"] == (MONTAG + timedelta(days=2)).isoformat() and "8.0 h" in ruhe["text"]
    assert c.post("/dienste", headers=h, json={"ressource_id": r, "datum": MONTAG.isoformat(), "beginn": "09:00", "ende": "08:00"}).status_code == 422
    assert c.post("/dienste", headers=h, json={"ressource_id": r, "datum": MONTAG.isoformat(), "schicht": "nacht"}).status_code == 422


def test_wochenvorschlag_ist_fair_und_uebernehmbar(umgebung):
    c, db, ids, h = umgebung
    naechster = MONTAG + timedelta(days=7)
    # Monteur B hat letzte Woche viel gearbeitet, ist am Dienstag abwesend
    for i in range(5):
        c.post("/dienste", headers=h, json={"ressource_id": ids["monteur_kaelte"], "datum": (MONTAG + timedelta(days=i)).isoformat(), "beginn": "06:00", "ende": "16:30", "pause_minuten": 30})
    c.post("/abwesenheiten", headers=h, json={"ressource_id": ids["monteur_kaelte"], "von": (naechster + timedelta(days=1)).isoformat(), "bis": (naechster + timedelta(days=1)).isoformat(), "art": "schulung"})
    v = c.get(f"/dienstplan/vorschlag?von={naechster.isoformat()}", headers=h).json()
    assert v["montag"] == naechster.isoformat() and len(v["tage"]) >= 5
    dienstag = next(t for t in v["tage"] if t["datum"] == (naechster + timedelta(days=1)).isoformat())
    assert all(d["name"] == "Monteur A" for d in dienstag["vorschlag"])        # B ist abwesend
    montag = next(t for t in v["tage"] if t["datum"] == naechster.isoformat())
    assert montag["vorschlag"][0]["name"] == "Monteur A"                         # weniger Stunden zuletzt → zuerst
    assert all(d["schicht"] == "tag" for d in v["dienste"])                      # kein Schichtbetrieb
    n = c.post("/dienstplan/vorschlag", headers=h, json={"dienste": v["dienste"]}).json()["angelegt"]
    assert n == len(v["dienste"]) > 0
    # zweiter Vorschlag für dieselbe Woche schlägt nichts Doppeltes vor
    assert c.get(f"/dienstplan/vorschlag?von={naechster.isoformat()}", headers=h).json()["dienste"] == []
    # Schichtbetrieb alterniert
    c.put("/betrieb/stammdaten", headers=h, json={"schichtbetrieb": True})
    with db.sitzungen() as s:
        s.query(Dienst).delete()
        s.commit()
    # zwei weitere Monteure, damit zwei je Tag eingeteilt werden
    c.post("/ressourcen", headers=h, json={"name": "Monteur C"})
    with db.sitzungen() as s:
        from core.modelle import Mandant
        m = s.get(Mandant, ids["mandant"])
        m.stammdaten = {**(m.stammdaten or {}), "schichtbetrieb": True}
        s.commit()
        v2 = dienstplan.vorschlag(s, m, naechster)
    assert set(d["schicht"] for d in v2["dienste"]) <= {"frueh", "spaet"} and v2["schichtbetrieb"]


def test_zeitkonto_und_urlaubskonto(umgebung):
    c, db, ids, h = umgebung
    r = ids["monteur_gas"]
    fall_id, _ = fall_mit_termin(c, h, ids, r)
    # Dienstag (heute): 9 h Dienst geplant, 6 h erfasst → −3 h; Montag Regel 8 h ohne Erfassung → −8 h; Mittwoch Urlaub → Gutschrift
    c.post("/dienste", headers=h, json={"ressource_id": r, "datum": HEUTE.isoformat(), "beginn": "07:00", "ende": "16:30", "pause_minuten": 30})
    c.post("/zeiten", headers=h, json={"art": "arbeit", "ressource_id": r, "servicefall_id": fall_id,
                                       "beginn": DEMO_ZEIT.replace(hour=7).isoformat(), "ende": DEMO_ZEIT.replace(hour=12).isoformat()})
    c.post("/zeiten", headers=h, json={"art": "fahrt", "ressource_id": r, "beginn": DEMO_ZEIT.replace(hour=12).isoformat(), "ende": DEMO_ZEIT.replace(hour=13).isoformat()})
    c.post("/abwesenheiten", headers=h, json={"ressource_id": r, "von": (HEUTE + timedelta(days=1)).isoformat(), "bis": (HEUTE + timedelta(days=1)).isoformat(), "art": "urlaub"})
    k = c.get(f"/zeitkonto?ressource_id={r}&von={MONTAG.isoformat()}&bis={(HEUTE + timedelta(days=1)).isoformat()}", headers=h).json()[0]
    assert k["soll_h"] == 8 + 9 + 8 and k["ist_h"] == 6 and k["gutschrift_h"] == 8 and k["saldo_h"] == -11
    tag = next(t for t in k["tage"] if t["datum"] == HEUTE.isoformat())
    assert tag["soll_h"] == 9 and tag["ist_h"] == 6
    c.patch(f"/ressourcen/{r}", headers=h, json={"urlaubsanspruch_tage": 28})
    c.post("/abwesenheiten", headers=h, json={"ressource_id": r, "von": "2026-07-20", "bis": "2026-07-31", "art": "urlaub"})
    u = next(x for x in c.get("/urlaub?jahr=2026", headers=h).json() if x["ressource_id"] == r)
    assert u["anspruch"] == 28 and u["genommen"] == 10 and u["geplant"] == 1 and u["rest"] == 17


# --- Zähler -------------------------------------------------------------------------------------------
def test_gradtag_normierung_und_anomalie(umgebung):
    c, db, ids, h = umgebung
    k = ids["kunde"]
    assert abs(zaehler.gradtag_anteil(date(2024, 12, 31), date(2025, 12, 31)) - 1.0) < 0.01
    winter = zaehler.gradtag_anteil(date(2025, 11, 30), date(2026, 2, 28))      # Dez–Feb
    sommer = zaehler.gradtag_anteil(date(2025, 5, 31), date(2025, 8, 31))       # Jun–Aug
    assert winter > 0.4 and sommer < 0.05
    # Ein Jahr Normalverbrauch 20 000 kWh, dann ein Winter mit +40 %
    staende = [("2024-12-31", 100000), ("2025-03-31", 109000), ("2025-06-30", 112000), ("2025-09-30", 113200), ("2025-12-31", 120000), ("2026-03-31", 132600)]
    for d, w in staende:
        r = c.post(f"/kunden/{k}/zaehlerstaende", headers=h, json={"art": "gas_kwh", "wert": w, "datum": d, "anlage_id": ids["anlage"]})
        assert r.status_code == 201, r.text
    # rückläufiger Stand wird abgewiesen, Zählerwechsel erlaubt
    assert c.post(f"/kunden/{k}/zaehlerstaende", headers=h, json={"art": "gas_kwh", "wert": 5, "datum": "2026-04-30", "anlage_id": ids["anlage"]}).status_code == 422
    assert c.post(f"/kunden/{k}/zaehlerstaende", headers=h, json={"art": "gas_kwh", "wert": 5, "datum": "2026-04-30", "anlage_id": ids["anlage"], "notiz": "Zählerwechsel"}).status_code == 201
    a = c.get(f"/kunden/{k}/zaehlerstaende", headers=h).json()
    reihe = next(r for r in a["reihen"] if r["art"] == "gas_kwh")
    assert reihe["klimabereinigt"] and reihe["bewertung"] == "auffaellig" and reihe["abweichung"] > 0.15
    assert 17000 < reihe["referenz"] < 23000
    assert a["hinweise"] and "hydraulischer Abgleich" in a["hinweise"][0]["text"]
    # Takten
    for d, bs, st in [("2025-10-01", 30000, 10000), ("2026-03-31", 36000, 10700)]:
        c.post(f"/kunden/{k}/zaehlerstaende", headers=h, json={"art": "brennerstarts", "wert": bs, "datum": d, "anlage_id": ids["anlage"]})
        c.post(f"/kunden/{k}/zaehlerstaende", headers=h, json={"art": "betriebsstunden", "wert": st, "datum": d, "anlage_id": ids["anlage"]})
    a = c.get(f"/kunden/{k}/zaehlerstaende", headers=h).json()
    assert any(x["art"] == "takten" for x in a["hinweise"])
    assert c.get("/zaehler/auffaellig", headers=h).json()[0]["kunde_id"] == k
    assert c.post(f"/kunden/{k}/zaehlerstaende", headers=h, json={"art": "unbekannt", "wert": 1}).status_code == 422


# --- Garantie -----------------------------------------------------------------------------------------
def test_gewaehrleistung_aus_eigenem_rapport_und_herstellergarantie(umgebung):
    c, db, ids, h = umgebung
    fall_id, termin_id = fall_mit_termin(c, h, ids, ids["monteur_gas"])
    with db.sitzungen() as s:
        from core.modelle import Servicefall
        s.get(Servicefall, fall_id).anlage_id = ids["anlage"]
        s.commit()
    c.post(f"/servicefaelle/{fall_id}/rapport", headers=h, json={"taetigkeiten": "Umwälzpumpe getauscht", "arbeitszeit_minuten": 60, "termin_id": termin_id,
                                                                  "ressource_id": ids["monteur_gas"], "material": [{"menge": "1", "bezeichnung": "Umwälzpumpe UPM3"}]})
    with db.sitzungen() as s:
        from core.modelle import Servicefall
        s.get(Servicefall, fall_id).anliegen = "Austausch Umwälzpumpe"
        s.commit()
    g = c.get(f"/kunden/{ids['kunde']}/gewaehrleistung", headers=h).json()
    assert g["leistungen"] and g["leistungen"][0]["jahre"] == 5 and "634a" in g["leistungen"][0]["grundlage"]
    assert g["herstellergarantien"] == [] or not g["herstellergarantien"][0]["aktiv"]   # Baujahr 2012 + 2 Jahre
    # neue Störung an derselben Anlage → Hinweis + Ereignis
    f2 = c.post("/servicefaelle", headers=h, json={"kunde_id": ids["kunde"], "anlage_id": ids["anlage"], "anliegen": "Pumpe läuft nicht, Heizung kalt"}).json()
    p = c.get(f"/servicefaelle/{f2['id']}/garantie", headers=h).json()
    assert p["moeglicher_gewaehrleistungsfall"] and "Gewährleistung" in p["hinweise"][0]
    ereignisse = c.get(f"/servicefaelle/{f2['id']}", headers=h).json()["ereignisse"]
    assert any(e["typ"] == "garantie.hinweis" for e in ereignisse)
    # Herstellergarantie per Frist auf die Anlage
    c.post("/fristen", headers=h, json={"bezug_typ": "anlage", "bezug_id": ids["anlage"], "art": "garantie", "bezeichnung": "Herstellergarantie 10 Jahre Wärmetauscher", "faellig": "2029-06-30"})
    p = c.get(f"/servicefaelle/{f2['id']}/garantie", headers=h).json()
    assert p["herstellergarantie"]["aktiv"] and any("Herstellergarantie" in x for x in p["hinweise"])
    # VOB-Stammdatum verkürzt auf 4 Jahre
    c.put("/betrieb/stammdaten", headers=h, json={"vob": True})
    assert c.get(f"/kunden/{ids['kunde']}/gewaehrleistung", headers=h).json()["leistungen"][0]["jahre"] == 4


# --- Qualifikationen ----------------------------------------------------------------------------------
def test_qualifikationsmatrix_und_ablauf_wirkt_auf_disposition(umgebung):
    c, db, ids, h = umgebung
    c.app.state.router = Router(None)
    abgelaufen = (HEUTE - timedelta(days=10)).isoformat()
    bald = (HEUTE + timedelta(days=30)).isoformat()
    r = c.patch(f"/ressourcen/{ids['monteur_gas']}", headers=h, json={"qualifikationen_ablauf": {"gas": abgelaufen, "trinkwasser": bald}}).json()
    assert r["qualifikationen_ablauf"] == {"gas": abgelaufen, "trinkwasser": bald}
    mtx = c.get("/qualifikationen/matrix", headers=h).json()
    zeile = next(z for z in mtx["zeilen"] if z["ressource_id"] == ids["monteur_gas"])
    assert zeile["zellen"]["gas"]["status"] == "abgelaufen" and zeile["zellen"]["trinkwasser"]["status"] == "laeuft_ab" and zeile["zellen"]["erste_hilfe"]["status"] == "fehlt"
    assert mtx["warnungen"][0]["qualifikation"] == "gas" and mtx["abdeckung"]["gas"] == 0
    assert any("Gasinstallation" in x for x in mtx["risiken"])
    # abgelaufene Qualifikation zählt in der Disposition nicht mehr
    assert c.get("/disposition/zeitfenster?qualifikation=gas", headers=h).json() == []
    assert c.get("/disposition/zeitfenster?qualifikation=trinkwasser", headers=h).json()
    # Verlängerung über PATCH (Zusammenführung, None löscht)
    c.patch(f"/ressourcen/{ids['monteur_gas']}", headers=h, json={"qualifikationen_ablauf": {"gas": "2031-01-01", "trinkwasser": None}})
    with db.sitzungen() as s:
        assert s.get(Ressource, ids["monteur_gas"]).qualifikationen_ablauf == {"gas": "2031-01-01"}
    assert c.get("/disposition/zeitfenster?qualifikation=gas", headers=h).json()
    assert "gas" in c.get("/qualifikationen/katalog", headers=h).json()


# --- Portal „Ihr Haus“ --------------------------------------------------------------------------------
def test_portal_zeigt_haus_und_nimmt_zaehlerstand_an(umgebung):
    c, db, ids, h = umgebung
    fall_id, _ = fall_mit_termin(c, h, ids, ids["monteur_gas"])
    link = c.get(f"/servicefaelle/{fall_id}/portal", headers=h).json()
    token = link["pfad"].rsplit("/", 1)[-1]
    d = c.get(f"/portal/faelle/{fall_id}?token={token}").json()
    assert d["haus"]["anlagen"][0]["typ"] == "Gas-Brennwertkessel" and d["haus"]["ueberfaellig"] and "gas_kwh" in d["haus"]["zaehlerarten"]
    z = c.post(f"/portal/faelle/{fall_id}/zaehlerstand?token={token}", json={"art": "gas_kwh", "wert": 123456, "anlage_id": ids["anlage"]})
    assert z.status_code == 200 and z.json()["quelle"] == "kunde"
    assert c.post(f"/portal/faelle/{fall_id}/zaehlerstand?token=falsch", json={"art": "gas_kwh", "wert": 1}).status_code in (401, 403, 404)
    assert c.get(f"/kunden/{ids['kunde']}/zaehlerstaende", headers=h).json()["staende"][0]["quelle"] == "kunde"
    d = c.get(f"/portal/faelle/{fall_id}?token={token}").json()
    assert d["haus"]["letzte_staende"][0]["wert"] == 123456
