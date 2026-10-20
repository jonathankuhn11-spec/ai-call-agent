from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from rdflib import Graph

from core import domaene
from core.demo import DEMO_ZEIT, referenzfluss
from core.modelle import Mandant, OutboxEintrag
from core.regeln import dringlichkeit_bewerten, telefon_normalisieren
from core.zustellung import MAX_VERSUCHE, abarbeiten

BERLIN = ZoneInfo("Europe/Berlin")


def test_referenzfluss_von_anruf_bis_outbox():
    e = referenzfluss()
    assert e["kunde_erkannt"] and e["dringlichkeit"] == "hoch"
    assert e["termin_vor_freigabe_verbindlich"] is False      # Klasse B: erst nach Büro-Freigabe
    assert e["status_servicefall"] == "eingeplant"
    assert e["ereignisse"] == ["servicefall.eingegangen", "servicefall.qualifiziert", "servicefall.eingeplant"]
    assert e["zustellung"]["zugestellt"] == 2                 # Fachsoftware + Terminbestätigung
    assert e["ontologie_tripel"] > 10


def test_statusautomat_verbietet_spruenge(umgebung):
    c, _, ids, h = umgebung
    f = c.post("/servicefaelle", headers=h, json={"kunde_id": ids["kunde"], "anliegen": "Wasserhahn tropft"}).json()
    r = c.post(f"/servicefaelle/{f['id']}/status", headers=h, json={"ziel": "abgerechnet"})
    assert r.status_code == 409 and "nicht zulässig" in r.json()["detail"]


def test_klasse_c_nie_automatisch_auch_wenn_policy_auto(umgebung):
    _, db, ids, _ = umgebung
    with db.sitzungen() as s:
        m = s.get(Mandant, ids["mandant"])
        m.policies = {"rechnung_erstellen": "auto", "termin_bestaetigen": "auto"}
        assert domaene.automatisch_freigegeben(m, "termin_bestaetigen")
        assert not domaene.automatisch_freigegeben(m, "rechnung_erstellen")
        f = domaene.aktion_beantragen(s, m, "rechnung_erstellen", {"betrag": 100}, "ki")
        assert f.status == "offen"


def test_dublette_ueber_rufnummer(umgebung):
    c, _, _, h = umgebung
    r = c.post("/kunden", headers=h, json={"name": "Doppelt", "telefon": "+49 221 1234567"})
    assert r.status_code == 409


def test_mandantentrennung(umgebung):
    c, _, ids, _ = umgebung
    fremd = c.post("/mandanten", json={"name": "Anderer Betrieb"}).json()
    r = c.get(f"/kunden/{ids['kunde']}", headers={"X-Mandant": fremd["id"]})
    assert r.status_code == 404


def test_zeitfenster_beachten_qualifikation_und_belegung(umgebung):
    c, _, ids, h = umgebung
    fenster = c.get("/disposition/zeitfenster", headers=h, params={"qualifikation": "kaeltetechnik"}).json()
    assert fenster and {f["ressource_id"] for f in fenster} == {ids["monteur_kaelte"]}
    f = c.post("/servicefaelle", headers=h, json={"kunde_id": ids["kunde"], "anliegen": "Klima prüfen",
                                                  "benoetigte_qualifikation": "kaeltetechnik"}).json()
    erstes = fenster[0]
    assert c.post(f"/servicefaelle/{f['id']}/termine", headers=h,
                  json={"ressource_id": erstes["ressource_id"], "beginn": erstes["beginn"]}).status_code == 201
    danach = c.get("/disposition/zeitfenster", headers=h, params={"qualifikation": "kaeltetechnik"}).json()
    beginn = datetime.fromisoformat(erstes["beginn"])
    for z in danach:  # kein Fenster überlappt den gebuchten Termin inklusive 30 min Puffer
        assert datetime.fromisoformat(z["beginn"]) >= beginn + timedelta(minutes=120) or \
            datetime.fromisoformat(z["ende"]) <= beginn - timedelta(minutes=30)
    doppelt = c.post(f"/servicefaelle/{f['id']}/termine", headers=h,
                     json={"ressource_id": erstes["ressource_id"], "beginn": erstes["beginn"]})
    assert doppelt.status_code == 409


def test_termin_nur_mit_passender_qualifikation(umgebung):
    c, _, ids, h = umgebung
    f = c.post("/servicefaelle", headers=h, json={"kunde_id": ids["kunde"], "anlage_id": ids["anlage"],
                                                  "anliegen": "Therme wartet"}).json()
    assert f["benoetigte_qualifikation"] == "gas"
    r = c.post(f"/servicefaelle/{f['id']}/termine", headers=h,
               json={"ressource_id": ids["monteur_kaelte"], "beginn": "2026-11-18T09:00:00+01:00"})
    assert r.status_code == 409


def test_zeitfenster_ausserhalb_arbeitszeit_und_am_wochenende(umgebung):
    c, _, _, h = umgebung
    samstag = datetime(2026, 11, 21, 6, 0, tzinfo=BERLIN)
    fenster = c.get("/disposition/zeitfenster", headers=h, params={"ab": samstag.isoformat(), "tage": 2}).json()
    for z in fenster:
        b = datetime.fromisoformat(z["beginn"]).astimezone(BERLIN)
        assert b.weekday() < 5 and b.hour >= 7


def test_gasgeruch_ist_sicherheitsfall_ohne_terminangebot(umgebung):
    c, _, _, h = umgebung
    r = c.post("/voice/zeitfenster", headers=h, json={"anliegen": "Im Keller riecht es nach Gas"}).json()
    assert r["sicherheitsfall"] and "Gasnetzbetreiber" in r["hinweis"] and r["zeitfenster"] == []


def test_regeln():
    assert telefon_normalisieren("0221 / 12 34-567") == "+492211234567"
    assert telefon_normalisieren("0049 221 1234567") == "+492211234567"
    assert telefon_normalisieren("abc") is None
    assert dringlichkeit_bewerten("Wasserrohrbruch im Bad", 6).dringlichkeit == "notdienst"
    assert dringlichkeit_bewerten("Heizung geht nicht", 1).dringlichkeit == "hoch"
    assert dringlichkeit_bewerten("Heizung geht nicht", 7).dringlichkeit == "normal"


def test_outbox_idempotent_und_dead_letter(umgebung):
    _, db, ids, _ = umgebung

    class Kaputt:
        name = "fachsoftware"

        def zustellen(self, eintrag):
            raise ConnectionError("Zielsystem nicht erreichbar")

    with db.sitzungen() as s:
        a = domaene.outbox_schreiben(s, ids["mandant"], "fachsoftware", "auftrag_anlegen", {}, "auftrag:1")
        b = domaene.outbox_schreiben(s, ids["mandant"], "fachsoftware", "auftrag_anlegen", {}, "auftrag:1")
        assert a is b
        s.commit()
        zeit = DEMO_ZEIT
        for _ in range(MAX_VERSUCHE):
            abarbeiten(s, {"fachsoftware": Kaputt()}, zeit)
            zeit += timedelta(hours=2)  # über jedes Backoff hinaus
        e = s.get(OutboxEintrag, a.id)
        assert e.status == "tot" and e.versuche == MAX_VERSUCHE and "ConnectionError" in e.letzter_fehler


def test_digital_twin_anlage_und_betrieb(umgebung):
    c, _, ids, h = umgebung
    z = c.get(f"/twin/anlagen/{ids['anlage']}", headers=h).json()
    assert z["wartung"]["status"] == "ueberfaellig" and z["wartung"]["benoetigte_qualifikation"] == "gas"
    b = c.get("/twin/betrieb", headers=h).json()
    assert b["wartungspotenzial"][0]["anlage_id"] == ids["anlage"]
    assert {a["name"] for a in b["auslastung"]} == {"Monteur A", "Monteur B"}


def test_ontologie_export_ist_abfragbar(umgebung):
    c, _, ids, h = umgebung
    g = Graph().parse(data=c.get("/ontologie/export", headers=h, params={"mit_tbox": True}).text, format="turtle")
    ergebnis = list(g.query("""
        PREFIX shk: <https://w3id.org/shk-backoffice/ont#>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT ?anlage ?monteur WHERE {
          ?anlage a ?klasse . ?klasse rdfs:subClassOf* shk:Anlage ; shk:erfordertQualifikation ?q .
          ?monteur shk:hatQualifikation ?q .
        }"""))
    assert len(ergebnis) == 1 and str(ergebnis[0][0]).endswith(ids["anlage"])


def test_api_key_wird_erzwungen(monkeypatch):
    monkeypatch.setenv("CORE_API_KEY", "geheim")
    from fastapi.testclient import TestClient

    from core.api import app_erzeugen
    from core.db import Datenbank
    c = TestClient(app_erzeugen(Datenbank("sqlite://")))
    assert c.post("/mandanten", json={"name": "x"}).status_code == 401
    assert c.post("/mandanten", json={"name": "x"}, headers={"X-API-Key": "geheim"}).status_code == 201
