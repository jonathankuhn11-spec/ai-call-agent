"""Aufbewahrung und Betroffenenrechte: Fristen laufen täglich im Planer, Auskunft und Löschung je Telefonnummer.

Kennzahlen überleben die Fristen (Anrufzeilen ohne Transkript), Inhalte nicht. Eine Löschung
pseudonymisiert den Lead, entfernt Inhalte und lässt die Nummer auf der Sperrliste.
"""
import json

from conftest import TOOL_SECRET
from integrations import kennzahlen
from integrations.aufbewahrung import Aufbewahrung, Fristen, nummer_hash
from integrations.konfig import Konfig


def pilot(sim, crm):
    for lead_id, persona in enumerate(["happy_path", "mieter", "keine_zeit", "opt_out"], start=1):
        sim.gespraech(crm.lead(lead_id), persona)


def test_retention_periods_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("AUFBEWAHRUNG_TRANSKRIPTE_TAGE", "14")
    monkeypatch.setenv("AUFBEWAHRUNG_PROTOKOLL_TAGE", "0")
    konfig = Konfig.aus_umgebung()
    assert konfig.fristen == {"transkripte": 14, "protokoll": 0}
    f = Fristen.aus_konfig(konfig)
    assert f.transkripte == 14 and f.protokoll == 0 and f.tickets == 90 and f.zustellungen == 30
    assert Fristen.aus_konfig(Konfig()).als_dict() == {"transkripte": 30, "tickets": 90, "verifizierungen": 30, "zustellungen": 30,
                                                       "webhooks": 90, "erinnerungen": 90, "protokoll": 365}


def test_contents_expire_but_the_numbers_stay(app, sim, uhr, kundensystem):
    crm = app.state.ctx.crm
    pilot(sim, crm)
    app.state.ctx.planer.tick()                                                 # Outbox zugestellt, Fristen geprüft (nichts fällig)
    assert crm.con.execute("SELECT COUNT(*) FROM calls WHERE transkript IS NOT NULL").fetchone()[0] == 4
    vorher = kennzahlen.kennzahlen(crm, tage=7)
    ab = app.state.ctx.aufbewahrung
    assert ab.anwenden()["entfernt"] == {"transkripte": 0, "tickets": 0, "verifizierungen": 0, "zustellungen": 0,
                                          "webhooks": 0, "erinnerungen": 0, "protokoll": 0}
    uhr.vor(days=31)
    bericht = ab.anwenden()
    assert bericht["entfernt"]["transkripte"] == 4 and bericht["entfernt"]["zustellungen"] == len(kundensystem["empfangen"])
    assert bericht["entfernt"]["webhooks"] == 0 and bericht["fristen_tage"]["transkripte"] == 30
    assert crm.con.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 4                       # Zeilen bleiben
    assert crm.con.execute("SELECT COUNT(*) FROM calls WHERE transkript IS NOT NULL").fetchone()[0] == 0
    assert app.state.ctx.outbox.status() == {}                                                     # zugestellte Ereignisse weg
    nachher = kennzahlen.kennzahlen(crm, tage=60)
    assert nachher["anrufe"] == vorher["anrufe"] == 4 and nachher["ergebnisse"] == vorher["ergebnisse"]
    assert ab.anwenden()["entfernt"]["transkripte"] == 0                                           # idempotent
    uhr.vor(days=60)
    assert ab.anwenden()["entfernt"]["webhooks"] == 4                                              # Dedup-Einträge nach 90 Tagen
    uhr.vor(days=300)
    assert ab.anwenden()["entfernt"]["protokoll"] == 4 and crm.con.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0


def test_the_planner_applies_the_periods_once_a_day(app, uhr, client):
    planer = app.state.ctx.planer
    assert "entfernt" in planer.tick()["aufbewahrung"]                            # erster Durchlauf
    assert "aufbewahrung" not in planer.tick()                                     # heute schon gelaufen
    uhr.vor(hours=23)
    assert "aufbewahrung" not in planer.tick()
    uhr.vor(hours=1)
    assert planer.tick()["aufbewahrung"]["zeit"] == "2026-10-14T08:00:00.000Z"
    m = client.get("/metrics", headers={"Authorization": f"Bearer {TOOL_SECRET}"}).json()["aufbewahrung"]
    assert m["letzter_lauf"] == "2026-10-14T08:00:00.000Z" and m["fristen_tage"]["transkripte"] == 30
    assert planer.tick(aufbewahrung=True)["aufbewahrung"]["zeit"] == "2026-10-14T08:00:00.000Z"   # erzwungen


def test_ticket_texts_and_verification_attempts_expire(app, uhr):
    crm = app.state.ctx.crm
    crm.ticket_anlegen(1, "c1", "stoerung_internet", "Router blinkt rot seit heute früh")
    crm.verifizierung_merken("c1", 1, False)
    ab = app.state.ctx.aufbewahrung
    uhr.vor(days=31)
    assert ab.anwenden()["entfernt"] == {"transkripte": 0, "tickets": 0, "verifizierungen": 1, "zustellungen": 0,
                                          "webhooks": 0, "erinnerungen": 0, "protokoll": 0}
    uhr.vor(days=60)
    assert ab.anwenden()["entfernt"]["tickets"] == 1
    t = crm.tickets(1)[0]
    assert t["text"] == "" and t["kategorie"] == "stoerung_internet"                 # Kategorie bleibt für die Statistik


def test_subject_access_request_returns_everything_about_a_number(app, sim):
    crm = app.state.ctx.crm
    pilot(sim, crm)
    crm.telli_kontakt_speichern(1, "ct_1", "loop_1")
    crm.con.execute("UPDATE leads SET crm_id = 'hs_77' WHERE id = 1")
    aus = app.state.ctx.aufbewahrung.auskunft("0251 000001")
    assert aus["telefon"] == "+49251000001" and aus["lead"]["name"] == "Thomas Becker" and aus["gesperrt"] is False
    assert len(aus["anrufe"]) == 1 and aus["anrufe"][0]["ergebnis"] == "termin_gebucht" and aus["anrufe"][0]["transkript"]
    assert len(aus["termine"]) == 1 and aus["notizen"] and aus["plattform_kontakt"]["contact_id"] == "ct_1"
    assert aus["kunden_crm_id"] == "hs_77" and aus["stand"] == "2026-10-13T08:00:00.000Z"
    json.dumps(aus)                                                                # serialisierbar für die Auskunft
    assert app.state.ctx.aufbewahrung.auskunft("+49 999 0")["lead"] is None
    gesperrt = app.state.ctx.aufbewahrung.auskunft(crm.lead(4)["telefon"])
    assert gesperrt["gesperrt"] is True and gesperrt["lead"]["status"] == "opt_out"


def test_erasure_pseudonymises_the_lead_and_keeps_the_number_blocked(app, sim, uhr, kundensystem):
    crm = app.state.ctx.crm
    pilot(sim, crm)
    app.state.ctx.planer.tick()
    crm.telli_kontakt_speichern(1, "ct_1", "loop_1")
    crm.con.execute("UPDATE leads SET crm_id = 'hs_77' WHERE id = 1")
    slot = crm.termin(1)["slot_id"]
    ereignisse_vorher = app.state.ctx.outbox.status()["zugestellt"]
    r = app.state.ctx.aufbewahrung.loeschen("+49 251 000001", "E-Mail vom 13.10.")
    assert r["lead"] == 1 and r["sperrliste"] is True
    assert r["entfernt"]["termine_abgesagt"] == 1 and r["entfernt"]["transkripte"] == 1 and r["entfernt"]["notizen"] >= 1
    assert r["beim_kunden_zu_loeschen"] == [{"system": "Kunden-CRM", "id": "hs_77"},
                                            {"system": "telli (Kontakt, Anrufe, Aufzeichnungen)", "id": "ct_1"}]
    lead = crm.lead(1)
    assert lead["name"] == "gelöscht" and lead["telefon"] is None and lead["plz"] is None and lead["status"] == "geloescht"
    assert lead["eigentuemer"] is None and lead["gebaeudetyp"] is None
    assert crm.lead_per_telefon("+49251000001") is None and crm.gesperrt("+49 251 000001")
    assert crm.termin(1) is None and crm.con.execute("SELECT lead_id FROM slots WHERE id = ?", [slot]).fetchone()[0] is None
    assert crm.notizen(1) == [] and crm.aufgaben(1) == [] and crm.erinnerungen(1) == [] and crm.telli_kontakt(1) is None
    assert crm.con.execute("SELECT COUNT(*) FROM calls WHERE lead_id = 1").fetchone()[0] == 1          # Zeile bleibt
    assert crm.con.execute("SELECT transkript FROM calls WHERE lead_id = 1").fetchone()[0] is None
    assert app.state.ctx.outbox.status()["zugestellt"] < ereignisse_vorher                             # Ereignisse des Leads weg
    assert all('"lead_id":1,' not in e["body"] for e in app.state.ctx.outbox.eintraege())
    assert crm.lead(2)["name"] == "Sabine Wolf" and crm.con.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 4
    protokoll = app.state.ctx.aufbewahrung.loeschungen()
    assert len(protokoll) == 1 and protokoll[0]["grund"] == "E-Mail vom 13.10." and protokoll[0]["lead_id"] == 1
    assert protokoll[0]["nummer_hash"] == nummer_hash("0251000001") and "000001" not in json.dumps(protokoll, default=str)
    assert sim.contact_lookup("+49 251 000001") == {"contact": None}                                 # Rückruf: unbekannt
    assert all(l["id"] != 1 for l in crm.leads_fuer_push())                                          # nie wieder im Dialer
    r2 = app.state.ctx.aufbewahrung.loeschen("+49 251 000001")                                       # Wiederholung: nur Sperre
    assert r2["lead"] is None and r2["sperrliste"] is True and len(app.state.ctx.aufbewahrung.loeschungen()) == 2
    assert kennzahlen.kennzahlen(crm, tage=7)["anrufe"] == 4                                          # Kennzahlen unverändert


def test_erasure_with_a_calendar_cancels_the_external_booking(con, konfig, kundensystem, telli_attrappe, quiet, uhr):
    from datetime import datetime, timezone

    from fastapi.testclient import TestClient

    import agent
    from integrations.outbox import Outbox
    from integrations.server import erstelle_app
    from integrations.simulator import CalComAttrappe, PlattformSimulator
    from conftest import API_KEY, KUNDEN_SECRET, WEBHOOK_SECRET

    calcom = CalComAttrappe([datetime(2026, 10, 14, 6, 30, tzinfo=timezone.utc), datetime(2026, 10, 14, 8, 30, tzinfo=timezone.utc)])
    con.execute("DELETE FROM slots")
    agent.slots_anlegen(con, datetime(2026, 10, 13, 10, 0))
    outbox = Outbox(con, "https://kunde.example/webhooks", KUNDEN_SECRET, client=kundensystem["client"], jetzt=uhr)
    app = erstelle_app(konfig, con=con, outbox=outbox, telli_client=telli_attrappe["client"], planer_starten=False, uhr=uhr,
                       kalender=calcom.kalender())
    with TestClient(app) as client:
        sim = PlattformSimulator(client, API_KEY, WEBHOOK_SECRET, TOOL_SECRET, uhr=uhr)
        app.state.ctx.planer.tick()
        sim.gespraech(app.state.ctx.crm.lead(1), "happy_path")
        assert calcom.buchungen["bk_0001"]["status"] == "accepted"
        r = app.state.ctx.aufbewahrung.loeschen("+49 251 000001")
        assert {"system": "Kalenderdienst (Buchung, Absage läuft über den Planer)", "id": "bk_0001"} in r["beim_kunden_zu_loeschen"]
        app.state.ctx.planer.tick()
        assert calcom.buchungen["bk_0001"]["status"] == "cancelled"
        assert calcom.buchungen["bk_0001"]["cancellationReason"] == "Löschersuchen"


def test_cli_datenschutz(konfig, tmp_path, monkeypatch, capsys):
    from integrations import __main__ as cli
    konfig.db_pfad = str(tmp_path / "ds.duckdb")
    monkeypatch.setattr(cli.Konfig, "aus_umgebung", classmethod(lambda cls: konfig))
    cli.main(["datenschutz"])
    aus = capsys.readouterr().out
    assert "Fristen (Tage)" in aus and '"transkripte": 30' in aus
    cli.main(["datenschutz", "--auskunft", "+49 251 000002"])
    assert json.loads(capsys.readouterr().out)["lead"]["name"] == "Sabine Wolf"
    cli.main(["datenschutz", "--loeschen", "+49 251 000002", "--grund", "Brief"])
    assert json.loads(capsys.readouterr().out)["entfernt"]["lead_pseudonymisiert"] == 1
    cli.main(["datenschutz", "--aufraeumen"])
    assert "entfernt" in json.loads(capsys.readouterr().out)
    cli.main(["datenschutz"])
    assert "Brief" in capsys.readouterr().out


def test_a_late_call_ended_for_an_erased_lead_stores_nothing(app, sim, kundensystem):
    crm = app.state.ctx.crm
    sim.gespraech(crm.lead(1), "keine_zeit")
    app.state.ctx.aufbewahrung.loeschen("+49 251 000001")
    r = sim.call_ended("call_spaet", "1", "+49 251 000001", {"ergebnis": "termin_gebucht"})
    assert r.json() == {"status": "ignoriert", "grund": "lead_geloescht", "call_id": "call_spaet", "lead_id": 1}
    assert crm.con.execute("SELECT COUNT(*) FROM calls WHERE lead_id = 1").fetchone()[0] == 1          # nur der alte, ohne Transkript
    assert crm.notizen(1) == [] and crm.aufgaben(1) == [] and crm.lead(1)["status"] == "geloescht"
    assert sim.call_ended("call_spaet", "1", "+49 251 000001", {"ergebnis": "termin_gebucht"}).json()["status"] == "duplikat"


def test_the_erasure_log_uses_a_keyed_hash_when_a_pepper_is_configured(con, uhr):
    from integrations.crm import LocalCRM
    crm = LocalCRM(con, uhr=uhr)
    assert nummer_hash("0251 000001") == nummer_hash("+49251000001") != nummer_hash("+49251000001", "sehr-geheim")
    assert nummer_hash("+49251000001", "sehr-geheim") != nummer_hash("+49251000001", "anderer")
    Aufbewahrung(crm, uhr=uhr, pepper="sehr-geheim").loeschen("+49 251 000001")
    eintrag = Aufbewahrung(crm, uhr=uhr).loeschungen()[0]
    assert eintrag["nummer_hash"] == nummer_hash("+49251000001", "sehr-geheim") and len(eintrag["nummer_hash"]) == 24


def test_cli_erasure_cancels_calendar_bookings_immediately(konfig, tmp_path, monkeypatch, capsys):
    from datetime import datetime, timedelta, timezone

    import agent
    from integrations import __main__ as cli
    from integrations import kalender as kalender_modul
    from integrations.crm import LocalCRM
    from integrations.kalender import Kalenderspiegel
    from integrations.simulator import CalComAttrappe

    konfig.db_pfad = str(tmp_path / "ds-kal.duckdb")
    zeit = (datetime.now(timezone.utc) + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    calcom = CalComAttrappe([zeit])
    monkeypatch.setattr(kalender_modul, "kalender_aus_konfig", lambda k: calcom.kalender())
    monkeypatch.setattr(cli.Konfig, "aus_umgebung", classmethod(lambda cls: konfig))
    con = agent.init_db(path=konfig.db_pfad)                                   # Termin mit bestätigter Kalenderbuchung anlegen
    crm = LocalCRM(con)
    spiegel = Kalenderspiegel(crm, calcom.kalender())
    assert spiegel.spiegeln()["frei"] == 1
    tools = crm.tools(3)
    slot = crm.freie_slots(1)[0]
    tools.offered = {slot["slot_id"]}
    r = tools._buchen_ohne_qualifizierung(slot_id=slot["slot_id"], art="vor_ort")
    assert r["ok"]
    termin = crm.termin(3, zukunft=True)
    crm.kalender_auftrag(termin["id"], "buchen")
    assert spiegel.bestaetigen(termin["id"])["bestaetigt"] and calcom.buchungen["bk_0001"]["status"] == "accepted"
    con.close()
    cli.main(["datenschutz", "--loeschen", "+49 251 000003"])
    aus = capsys.readouterr().out
    assert '"id": "bk_0001"' in aus and '"storniert": 1' in aus
    assert calcom.buchungen["bk_0001"]["status"] == "cancelled" and calcom.buchungen["bk_0001"]["cancellationReason"] == "Löschersuchen"
