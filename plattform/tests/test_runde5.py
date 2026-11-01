"""Runde 5: Tourenoptimierung (VRPTW), Warteliste mit Nachbesetzung, Ausfallrisiko (Weibull), Nachfrageprognose
(Holt-Winters), lernende Dauer-Schätzung, Dublettenauflösung."""
import math
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from core import dubletten, prognose, risiko, schaetzung
from core.demo import DEMO_ZEIT
from core.modelle import Ereignis, Kunde, OutboxEintrag, Servicefall, Termin, Wartelisteneintrag
from core.optimierung import Auftrag, Fahrzeug, Problem
from core.routing import Router
from tests.test_einsatz import fall_mit_termin
from tests.test_finanz import STAMMDATEN

KOELN = (50.9375, 6.9603)


def kunde_mit_punkt(c, h, name, tel, lat, lon, **extra):
    return c.post("/kunden", headers=h, json={"name": name, "telefon": tel, "ort": "Köln", "plz": "50667", "lat": lat, "lon": lon, **extra}).json()


def termin_fuer(c, h, kunde_id, ressource_id, beginn, anliegen="Wartung Gastherme", **extra):
    f = c.post("/servicefaelle", headers=h, json={"kunde_id": kunde_id, "anliegen": anliegen, **extra}).json()
    t = c.post(f"/servicefaelle/{f['id']}/termine", headers=h, json={"ressource_id": ressource_id, "beginn": beginn.isoformat()})
    assert t.status_code == 201, t.text
    return f["id"], t.json()["termin"]["id"]


# --- Tourenoptimierung ------------------------------------------------------------------------------
def test_optimierer_kern_buendelt_touren_und_haelt_harte_constraints():
    matrix = [[abs(i - j) * 10.0 for j in range(7)] for i in range(7)]      # Betrieb 0, Kunden 1..6 auf einer Linie
    auftraege = [Auftrag(f"t{i}", f"f{i}", f"K{i}", 60, None, "normal", "vorgeschlagen", 0, 1440, i, "A" if i % 2 else "B", 480 + i * 90) for i in range(1, 7)]
    fahrzeuge = [Fahrzeug("A", "A", set(), 450, 1020, 0), Fahrzeug("B", "B", set(), 450, 1020, 0)]
    p = Problem(auftraege, fahrzeuge, matrix, 30.0, 30)
    start = [[0, 2, 4], [1, 3, 5]]
    ist = p.bewerten(start)[0]
    plan, kosten = p.loesen(start, 400)
    assert kosten < ist and all(0 <= i < 6 for r in plan for i in r) and sorted(i for r in plan for i in r) == list(range(6))
    # Qualifikation ist hart: Auftrag mit „gas“ landet nur bei A
    auftraege[2].qualifikation = "gas"
    fahrzeuge[0].qualifikationen = {"gas"}
    plan, kosten = p.loesen(start, 300)
    assert 2 in plan[0] and kosten < math.inf
    # bestätigter Termin mit Toleranz 0 behält seine Zeit
    auftraege[4].status, auftraege[4].fenster_von, auftraege[4].fenster_bis = "bestaetigt", 600, 600
    plan, _ = p.loesen(start, 300)
    _, teile = p.bewerten(plan)
    for f, bew in zip(fahrzeuge, teile):
        if "t5" in bew.zeiten:
            assert bew.zeiten["t5"][0] == 600 or bew.verspaetung > 0


def test_tagesplan_vorschlag_und_umsetzung(umgebung):
    c, db, ids, h = umgebung
    c.app.state.router = Router(None)
    c.put("/betrieb/stammdaten", headers=h, json={**STAMMDATEN, "lat": KOELN[0], "lon": KOELN[1]})
    nord = [kunde_mit_punkt(c, h, f"Nord {i}", f"0221 71000{i}", 50.99 + i * 0.01, 6.96) for i in range(2)]
    sued = [kunde_mit_punkt(c, h, f"Süd {i}", f"0221 72000{i}", 50.88 - i * 0.01, 6.96) for i in range(2)]
    tag = DEMO_ZEIT.replace(hour=8, minute=0)
    # verschränkt: A fährt Nord→Süd, B fährt Süd→Nord
    termine = [termin_fuer(c, h, nord[0]["id"], ids["monteur_gas"], tag), termin_fuer(c, h, sued[0]["id"], ids["monteur_gas"], tag + timedelta(hours=3)),
               termin_fuer(c, h, sued[1]["id"], ids["monteur_kaelte"], tag), termin_fuer(c, h, nord[1]["id"], ids["monteur_kaelte"], tag + timedelta(hours=3))]
    v = c.get("/plantafel/optimieren", headers=h, params={"tag": tag.date().isoformat()}).json()
    assert v["auftraege"] == 4 and v["quelle"] == "luftlinie" and v["ist"]["kosten"] is not None
    assert v["vorschlag"]["fahrzeit_minuten"] < v["ist"]["fahrzeit_minuten"] and v["ersparnis_minuten"] > 0 and v["aenderungen"]
    assert "weniger Fahrzeit" in v["begruendung"]
    for route in v["routen"]:
        folge = [s["kunde"].split()[0] for s in route["stopps"]]
        assert sum(1 for a, b in zip(folge, folge[1:]) if a != b) <= 1   # keine Tour pendelt zwischen Nord und Süd
    u = c.post("/plantafel/optimieren/umsetzen", headers=h, json={"plan": v}).json()
    assert u["verschoben"] and u["neue_bestaetigungen"] == 0
    with db.sitzungen() as s:
        assert s.scalar(select(Ereignis).where(Ereignis.typ == "tagesplan.umgesetzt")) is not None
        for _, tid in termine:
            assert s.get(Termin, tid).status == "vorgeschlagen"
    # erneuter Lauf: nichts mehr zu verbessern
    v2 = c.get("/plantafel/optimieren", headers=h, params={"tag": tag.date().isoformat()}).json()
    assert v2["aenderungen"] == [] and "bereits optimal" in v2["begruendung"]
    # bestätigter Termin (Toleranz 0) bleibt fix; Umsetzung mit neuer Zeit erzeugte Terminbestätigung
    fall_id, tid = termine[0]
    for fr in c.get("/freigaben", headers=h).json():
        if fr["aktion"] == "termin_bestaetigen" and fr["payload"].get("termin_id") == tid:
            c.post(f"/freigaben/{fr['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "buero"})
    v3 = c.get("/plantafel/optimieren", headers=h, params={"tag": tag.date().isoformat(), "toleranz_minuten": 0}).json()
    fest = next(st for r in v3["routen"] for st in r["stopps"] if st["termin_id"] == tid)
    assert not fest["verschoben"]


# --- Warteliste -------------------------------------------------------------------------------------
def test_warteliste_nachbesetzung_portal_annahme_und_verfall(umgebung):
    c, db, ids, h = umgebung
    c.put("/betrieb/stammdaten", headers=h, json={**STAMMDATEN, "basis_url": "https://shk.example.test", "warteliste_frist_stunden": 3})
    fall_a, termin_a = fall_mit_termin(c, h, ids, ids["monteur_gas"])             # bestätigt, heute 09:00
    k2 = c.post("/kunden", headers=h, json={"name": "Wartende Kundin", "telefon": "0221 9999", "email": "w@example.test"}).json()
    fall_b, termin_b = termin_fuer(c, h, k2["id"], ids["monteur_gas"], DEMO_ZEIT + timedelta(days=8, hours=1), "Heizung geht nicht")
    r = c.post(f"/servicefaelle/{fall_b}/warteliste", headers=h, json={"wunsch_von": DEMO_ZEIT.date().isoformat(), "wunsch_bis": (DEMO_ZEIT + timedelta(days=5)).date().isoformat(),
                                                                       "tageszeiten": ["vormittag"]})
    assert r.status_code == 201, r.text
    assert r.json()["prioritaet"] == 60 and r.json()["status"] == "offen"      # „geht nicht“ → hoch
    assert c.post(f"/servicefaelle/{fall_b}/warteliste", headers=h, json={"wunsch_von": "2026-11-17", "wunsch_bis": "2026-11-20"}).status_code == 409
    w = c.get("/warteliste", headers=h).json()
    assert w["offen"] == 1 and w["luecken"] and any(l["kandidaten"] >= 1 for l in w["luecken"])
    # Büro sagt Termin A ab → Lücke 09:00 wird der Wartenden angeboten
    r = c.post(f"/termine/{termin_a}/absagen", headers=h, json={"grund": "Kunde verhindert"})
    assert r.status_code == 200 and r.json()["status"] == "abgesagt"
    w = c.get("/warteliste", headers=h).json()
    e = next(x for x in w["eintraege"] if x["servicefall_id"] == fall_b)
    assert e["status"] == "angeboten" and e["angebot"]["ressource_id"] == ids["monteur_gas"]
    angeboten = datetime.fromisoformat(e["angebot"]["beginn"])
    assert angeboten.astimezone(timezone.utc).date() == DEMO_ZEIT.date() and angeboten.astimezone(timezone.utc).hour < 12
    frist = datetime.fromisoformat(e["angebot"]["frist"])
    assert frist - DEMO_ZEIT.astimezone(timezone.utc) <= timedelta(hours=3)
    with db.sitzungen() as s:
        ob = s.scalar(select(OutboxEintrag).where(OutboxEintrag.aktion == "nachricht", OutboxEintrag.idempotenz_schluessel.like("warteliste:%")))
        assert ob is not None and "früherer Termin" in ob.payload["text"] and "/portal/#" in ob.payload["text"]
    # Kundin bestätigt im Portal: Eintrag gebucht, späterer Termin B abgesagt, Freigabe termin_bestaetigen beantragt
    link = c.get(f"/servicefaelle/{fall_b}/portal", headers=h).json()
    token = link["pfad"].split("/")[-1]
    r = c.post(f"/portal/faelle/{fall_b}/termine/{e['angebot']['termin_id']}", params={"token": token}, json={"aktion": "bestaetigen"})
    assert r.status_code == 200, r.text
    with db.sitzungen() as s:
        we = s.get(Wartelisteneintrag, e["id"])
        assert we.status == "gebucht" and s.get(Termin, termin_b).status == "abgesagt"
    # Verfall: neuer Eintrag, Angebot mit abgelaufener Frist → zurück auf offen, Termin abgesagt, Zähler 1
    k3 = c.post("/kunden", headers=h, json={"name": "Dritter Kunde", "telefon": "0221 8888", "email": "d@example.test"}).json()
    fall_c, _ = termin_fuer(c, h, k3["id"], ids["monteur_gas"], DEMO_ZEIT + timedelta(days=9, hours=2), "Wartung")
    c.post(f"/servicefaelle/{fall_c}/warteliste", headers=h, json={"wunsch_von": DEMO_ZEIT.date().isoformat(), "wunsch_bis": (DEMO_ZEIT + timedelta(days=5)).date().isoformat()})
    luecke = next(l for l in c.get("/warteliste", headers=h).json()["luecken"] if l["bester"] and l["bester"]["kunde"] == "Dritter Kunde")
    a = c.post("/warteliste/anbieten", headers=h, params={"ressource_id": luecke["ressource_id"], "beginn": luecke["beginn"], "ende": luecke["ende"]}).json()
    assert a["kunde"] == "Dritter Kunde" and a["gruende"]
    with db.sitzungen() as s:
        we = s.get(Wartelisteneintrag, a["eintrag_id"])
        we.angebot = {**we.angebot, "frist": (DEMO_ZEIT - timedelta(hours=1)).isoformat()}
        s.commit()
    lauf = c.post("/tagesaufgaben", headers=h).json()
    assert lauf["warteliste_verfallen"] == 1
    with db.sitzungen() as s:
        we = s.get(Wartelisteneintrag, a["eintrag_id"])
        assert we.status == "offen" and we.angebote_anzahl == 1 and s.get(Termin, a["termin_id"]).status == "abgesagt"
    assert c.post(f"/warteliste/{a['eintrag_id']}/zurueckziehen", headers=h).json()["status"] == "zurueckgezogen"


# --- Ausfallrisiko ----------------------------------------------------------------------------------
def test_weibull_hazard_steigt_mit_alter_und_kovariaten():
    p = [risiko.ausfallwahrscheinlichkeit(a, 2.3, 18.0) for a in (2, 8, 14, 20, 26)]
    assert all(x < y for x, y in zip(p, p[1:])) and 0 < p[0] < 0.03 and p[-1] > 0.15
    assert risiko.ausfallwahrscheinlichkeit(14, 2.3, 18.0, 2.0) > p[2]
    assert risiko.klasse(0.01) == "niedrig" and risiko.klasse(0.2) == "hoch" and risiko.klasse(0.5) == "kritisch"


def test_risikoportfolio_erklaert_treiber_und_reagiert_auf_stoerungen(umgebung):
    c, db, ids, h = umgebung
    p = c.get("/risiko", headers=h).json()
    assert p["anzahl"] == 1
    a = p["anlagen"][0]
    assert a["anlagentyp"] == "gas_brennwert" and a["alter_jahre"] == 14 and "Wartung überfällig" in a["treiber"] and "Wartungsvertrag (−15 %)" in a["treiber"]
    vorher = a["p_12_monate"]
    for i in range(2):
        c.post("/servicefaelle", headers=h, json={"kunde_id": ids["kunde"], "anlage_id": ids["anlage"], "anliegen": "Heizung ausgefallen, kein Warmwasser", "dringlichkeit": "hoch"})
    a2 = c.get(f"/risiko/anlagen/{ids['anlage']}", headers=h).json()
    assert a2["p_12_monate"] > vorher and any("Störf" in t for t in a2["treiber"]) and a2["klasse"] in ("mittel", "hoch", "kritisch")
    assert any("Wartung vorziehen" in e for e in a2["empfehlungen"])
    p2 = c.get("/risiko", headers=h).json()
    assert p2["erwartete_ausfaelle_12_monate"] >= a2["p_12_monate"] - 0.01
    assert all(x["erwarteter_bedarf"] >= 0.3 for x in p2["ersatzteilvorhaltung"])


# --- Prognose ---------------------------------------------------------------------------------------
def test_holt_winters_trifft_synthetische_saison():
    saison = 12
    y = [100 + 2 * t + 20 * math.sin(2 * math.pi * t / saison) for t in range(48)]
    level, trend, sais, resid = prognose.holt_winters(y, saison, 0.3, 0.05, 0.3)
    vorhersage = [level + (h + 1) * trend + sais[(48 + h) % saison] for h in range(6)]
    wahr = [100 + 2 * t + 20 * math.sin(2 * math.pi * t / saison) for t in range(48, 54)]
    assert max(abs(a - b) for a, b in zip(vorhersage, wahr)) < 12


def test_auslastungsprognose_mit_prior_und_warnungen(umgebung):
    c, db, ids, h = umgebung
    p = c.get("/prognose/auslastung", headers=h, params={"wochen": 6}).json()
    assert len(p["wochen"]) == 6 and "Saisonprior" in p["verfahren"] and p["mittlere_dauer_h"] > 0
    w = p["wochen"][0]
    assert w["kapazitaet_h"] > 0 and w["untergrenze"] <= w["vorgaenge"] <= w["obergrenze"] and w["lage"] in ("engpass", "unterauslastung", "normal")
    reihe = [(date(2026, 1, 5) + timedelta(weeks=i), 0) for i in range(104)]
    leer = prognose.prognose_reihe(reihe, "handwerk", 4)
    assert all(x["vorgaenge"] == 0 for x in leer["wochen"])
    reihe = [(date(2024, 11, 18) + timedelta(weeks=i), int(10 + 5 * math.cos(2 * math.pi * i / 52))) for i in range(104)]
    hw = prognose.prognose_reihe(reihe, "handwerk", 8)
    assert hw["verfahren"].startswith("Holt-Winters") and 8 <= hw["wochen"][0]["vorgaenge"] <= 18


# --- Dauer-Schätzung ---------------------------------------------------------------------------------
def test_dauerschaetzung_lernt_aus_rapporten_und_fliesst_in_neue_vorgaenge(umgebung):
    c, db, ids, h = umgebung
    assert schaetzung.kategorie("Heizung geht nicht, kein warmes Wasser") == "stoerung" and schaetzung.kategorie("Jahreswartung Therme") == "wartung"
    assert schaetzung.kategorie("x", "notdienst") == "notdienst" and schaetzung.kategorie("Wärmepumpe einbauen") == "installation"
    s0 = c.get("/schaetzung/dauer", headers=h, params={"anliegen": "Wartung Gastherme", "anlagentyp": "gas_brennwert"}).json()
    assert s0["ebene"] == "standard" and s0["planung_minuten"] == 90 and s0["konfidenz"] == "niedrig"
    for i, minuten in enumerate((55, 60, 50, 65, 70)):
        fall_id, termin_id = termin_fuer(c, h, ids["kunde"], ids["monteur_gas"], DEMO_ZEIT + timedelta(days=i, hours=1), "Wartung Gastherme", anlage_id=ids["anlage"])
        for fr in c.get("/freigaben", headers=h).json():
            c.post(f"/freigaben/{fr['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "buero"})
        r = c.post(f"/servicefaelle/{fall_id}/rapport", headers=h, json={"taetigkeiten": "Wartung durchgeführt", "ressource_id": ids["monteur_gas"], "arbeitszeit_minuten": minuten})
        assert r.status_code == 201, r.text
    s1 = c.get("/schaetzung/dauer", headers=h, params={"anliegen": "Wartung Gastherme", "anlagentyp": "gas_brennwert"}).json()
    assert s1["ebene"] == "anlagentyp×kategorie" and s1["stichprobe"] == 5 and 60 <= s1["erwartung_minuten"] <= 75 and s1["planung_minuten"] in (75, 90)
    s2 = c.get("/schaetzung/dauer", headers=h, params={"anliegen": "Störung Wärmepumpe", "anlagentyp": "waermepumpe"}).json()
    assert s2["ebene"] == "betrieb" and s2["stichprobe"] == 0   # Prior nur aus dem Betriebsmittel
    f = c.post("/servicefaelle", headers=h, json={"kunde_id": ids["kunde"], "anlage_id": ids["anlage"], "anliegen": "Wartung Gastherme"}).json()
    assert f["dauer_minuten"] == s1["planung_minuten"]
    with db.sitzungen() as s:
        ev = s.scalar(select(Ereignis).where(Ereignis.typ == "servicefall.eingegangen", Ereignis.entitaet_id == f["id"]))
        assert ev.payload["dauer_schaetzung"]["ebene"] == "anlagentyp×kategorie"
    assert c.post("/servicefaelle", headers=h, json={"kunde_id": ids["kunde"], "anliegen": "Beratung", "dauer_minuten": 45}).json()["dauer_minuten"] == 45
    assert c.get("/schaetzung/uebersicht", headers=h).json()[0]["n"] == 5


# --- Dubletten --------------------------------------------------------------------------------------
def test_koelner_phonetik_jaro_winkler_und_adressnormalisierung():
    assert dubletten.koelner_phonetik("Müller-Lüdenscheidt") == "65752682" and dubletten.koelner_phonetik("Wikipedia") == "3412"
    assert dubletten.koelner_phonetik("Meyer") == dubletten.koelner_phonetik("Maier") == dubletten.koelner_phonetik("Mayr")
    assert dubletten.jaro_winkler("martha", "marhta") > 0.95 and dubletten.jaro_winkler("abc", "xyz") == 0
    assert dubletten.strasse_normal("Hauptstr. 12a") == dubletten.strasse_normal("Hauptstraße 12 a") == ("hauptstr", "12a")


def test_dubletten_finden_und_zusammenfuehren(umgebung):
    c, db, ids, h = umgebung
    a = c.post("/kunden", headers=h, json={"name": "Hans Meier", "telefon": "0221 4455", "strasse": "Hauptstr. 12a", "plz": "50667", "ort": "Köln", "email": "hm@example.test"}).json()
    c.post("/kunden", headers=h, json={"name": "Schulze GmbH", "telefon": "0221 6677", "strasse": "Ring 1", "plz": "50668", "ort": "Köln"}).json()
    with db.sitzungen() as s:   # Import ohne Dublettenschutz: gleiche Rufnummer, phonetisch gleicher Name, andere Schreibweise
        b = Kunde(mandant_id=ids["mandant"], name="Maier, Hans", telefon="+492214455", strasse="Hauptstraße 12 a", plz="50667", ort="Koeln", kartei={"zugang": "Schlüssel beim Nachbarn"},
                  einwilligungen={"sms": "2026-01-01T00:00:00Z"}, nicht_erschienen=1)
        s.add(b); s.commit(); b_id = b.id
        fall = Servicefall(mandant_id=ids["mandant"], kunde_id=b_id, anliegen="Altvorgang", status="abgeschlossen"); s.add(fall); s.commit(); fall_id = fall.id
    d = c.get("/dubletten", headers=h).json()
    assert len(d) == 1 and d[0]["sicher"] and d[0]["score"] >= 0.85 and {"gleiche Rufnummer", "Name phonetisch gleich", "gleiche Adresse"} <= set(d[0]["gruende"])
    assert d[0]["ziel"]["id"] == a["id"] and d[0]["quelle"]["id"] == b_id   # älterer Datensatz bleibt
    assert c.post(f"/kunden/{a['id']}/zusammenfuehren", headers=h, json={"quelle_id": a["id"]}).status_code == 422
    z = c.post(f"/kunden/{a['id']}/zusammenfuehren", headers=h, json={"quelle_id": b_id}).json()
    assert z["umgehaengt"]["servicefall.kunde_id"] == 1
    k = c.get(f"/kunden/{a['id']}", headers=h).json()["kunde"]
    assert k["kartei"] == {"zugang": "Schlüssel beim Nachbarn"} and k["einwilligungen"]["sms"] and k["nicht_erschienen"] == 1
    assert c.get(f"/kunden/{b_id}", headers=h).status_code == 404
    with db.sitzungen() as s:
        assert s.get(Servicefall, fall_id).kunde_id == a["id"]
        ev = s.scalar(select(Ereignis).where(Ereignis.typ == "kunde.zusammengefuehrt"))
        assert ev.payload["quelle"]["name"] == "Maier, Hans" and ev.payload["quelle_id"] == b_id
    assert c.get("/dubletten", headers=h).json() == []
