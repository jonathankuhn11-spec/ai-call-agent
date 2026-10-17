"""Use Case 2, Inbound-Service (telko_inbound): Verifizierungspflicht, Mindestvorlauf, Ticket, Termin-Lebenszyklus.

Dasselbe Backend wie im PV-Fall, andere Geschäftsregeln per Use-Case-Konfiguration. Jede Regel
wird vom Code erzwungen, nicht vom Prompt erbeten.
"""
from datetime import timedelta

import pytest
from conftest import API_KEY, KUNDEN_SECRET, TOOL_SECRET, WEBHOOK_SECRET


@pytest.fixture
def inbound(con, konfig, kundensystem, telli_attrappe, quiet, uhr):
    from zoneinfo import ZoneInfo

    from fastapi.testclient import TestClient

    import agent
    from integrations.outbox import Outbox
    from integrations.server import erstelle_app
    from integrations.simulator import PlattformSimulator

    konfig.usecase = "telko_inbound"
    con.execute("DELETE FROM slots")
    agent.slots_anlegen(con, uhr().astimezone(ZoneInfo(konfig.zeitzone)).replace(tzinfo=None))
    outbox = Outbox(con, "https://ticketsystem.example/webhooks", KUNDEN_SECRET, client=kundensystem["client"], jetzt=uhr)
    app = erstelle_app(konfig, con=con, outbox=outbox, telli_client=telli_attrappe["client"], planer_starten=False, uhr=uhr)
    with TestClient(app) as client:
        yield app, client, PlattformSimulator(client, API_KEY, WEBHOOK_SECRET, TOOL_SECRET, uhr=uhr)


def termin_anlegen(crm, lead_id, in_stunden=72):
    """Ein bestehender Techniker-Termin, wie ihn das Auftragssystem angelegt hätte (keine PV-Qualifizierung nötig)."""
    tools = crm.tools(lead_id)
    slot = crm.freie_slots(1)[0]
    crm.con.execute("UPDATE slots SET start = ? WHERE id = ?", [crm.jetzt() + timedelta(hours=in_stunden), slot["slot_id"]])
    tools.offered = {slot["slot_id"]}
    assert tools.book_appointment(slot_id=slot["slot_id"], art="vor_ort")["ok"]
    crm.con.execute("UPDATE appointments SET gebucht_am = gebucht_am - INTERVAL 3 DAY")     # vor Tagen gebucht, nicht im Anruf
    return slot["slot_id"]


def test_use_case_config_sets_the_rules(inbound):
    app, client, sim = inbound
    ctx = app.state.ctx
    assert ctx.usecase.id == "telko_inbound" and ctx.verifizierung_pflicht is True and ctx.crm.mindestvorlauf_h == 24
    d = client.get("/tools/definitions", headers={"Authorization": f"Bearer {TOOL_SECRET}"}).json()
    assert [t["name"] for t in d["telli_custom_tools"]] == ["verify_customer", "lookup_lead", "cancel_appointment", "check_slots",
                                                           "book_appointment", "create_ticket"]
    assert client.post("/tools/update_lead", json={"external_id": "1"}, headers={"Authorization": f"Bearer {TOOL_SECRET}"}).status_code == 404


def test_no_data_before_verification_and_three_attempts_max(inbound):
    app, client, sim = inbound
    crm = app.state.ctx.crm
    termin_anlegen(crm, 1)
    b = sim.gespraech(crm.lead(1), "nicht_verifiziert")
    assert b["contact_lookup"]["contact"]["first_name"] == "Thomas"                      # Nummer bekannt
    assert b["vor_verifizierung"]["ok"] is False and b["vor_verifizierung"]["verifizierung_noetig"]
    assert "naechster_termin" not in b["vor_verifizierung"]                                # keine Termindaten
    assert [v["verbleibende_versuche"] for v in b["versuche"]] == [2, 1, 0] and b["versuche"][-1]["weiterleiten"] is True
    assert b["nach_dreimal"]["ok"] is False and b["nach_dreimal"]["weiterleiten"] is True  # auch mit richtiger PLZ: zu spät
    assert b["call_ended"]["ergebnis"] == "nicht_verifiziert"
    assert crm.aufgaben(1)[0]["titel"] == "Prüfen: nicht_verifiziert" and crm.termin(1) is not None   # nichts verändert


def test_reschedule_with_notice_moves_the_appointment(inbound, kundensystem):
    app, client, sim = inbound
    crm = app.state.ctx.crm
    alt = termin_anlegen(crm, 1, in_stunden=72)
    b = sim.gespraech(crm.lead(1), "termin_verschieben")
    assert b["verify"]["verifiziert"] is True and b["lookup"]["naechster_termin"].endswith("Uhr")
    assert b["cancel"]["ok"] and b["book"]["ok"] and b["book"]["art"] == "vor_ort"
    assert b["call_ended"]["ergebnis"] == "termin_verschoben" and b["call_ended"]["termin"].endswith("Z")
    assert crm.termin(1)["slot_id"] != alt and crm.con.execute("SELECT lead_id FROM slots WHERE id = ?", [alt]).fetchone()[0] is None
    assert [e["status"] for e in crm.erinnerungen(1)] == ["entfallen", "offen"]
    app.state.ctx.planer.tick()
    typen = [e["typ"] for e in kundensystem["empfangen"]]
    assert typen == ["termin.abgesagt", "termin.gebucht", "anruf.beendet"]
    assert next(e for e in kundensystem["empfangen"] if e["typ"] == "termin.gebucht")["body"]["daten"]["umbuchung"] is True


def test_short_notice_cancellation_is_refused_by_code(inbound, kundensystem):
    app, client, sim = inbound
    crm = app.state.ctx.crm
    termin_anlegen(crm, 2, in_stunden=12)
    b = sim.gespraech(crm.lead(2), "termin_kurzfristig")
    assert b["cancel"]["ok"] is False and b["cancel"]["weiterleiten"] is True and "24 Stunden" in b["cancel"]["fehler"]
    assert crm.termin(2) is not None and crm.stornierungen(2) == []
    assert b["call_ended"]["ergebnis"] == "weitergeleitet" and "transfer" in b["call_ended"]["flags"]


def test_outage_becomes_a_ticket_event_for_the_ticket_system(inbound, kundensystem):
    app, client, sim = inbound
    crm = app.state.ctx.crm
    b = sim.gespraech(crm.lead(3), "stoerung")
    assert b["ticket"]["ok"] and b["ticket"]["ticket_nummer"] == "T-00001"
    assert b["call_ended"]["ergebnis"] == "ticket_erstellt"
    app.state.ctx.planer.tick()
    ticket = next(e for e in kundensystem["empfangen"] if e["typ"] == "ticket.erstellt")
    assert ticket["body"]["daten"]["ticket_nummer"] == "T-00001" and ticket["body"]["daten"]["kategorie"] == "stoerung_internet"
    assert ticket["body"]["daten"]["name"] == "Mehmet Yilmaz" and "Router" in ticket["body"]["daten"]["beschreibung"]
    assert crm.tickets(3)[0]["status"] == "offen"


def test_confirmation_changes_nothing_and_claims_without_evidence_are_flagged(inbound):
    app, client, sim = inbound
    crm = app.state.ctx.crm
    termin_anlegen(crm, 4)
    b = sim.gespraech(crm.lead(4), "termin_bestaetigen")
    assert b["call_ended"]["ergebnis"] == "termin_bestaetigt" and crm.termin(4) is not None
    # Agent behauptet eine Verschiebung, die es nicht gab
    call_id = "call_claim"
    sim.tool("verify_customer", call_id, "4", plz=crm.lead(4)["plz"])
    r = sim.call_ended(call_id, "4", crm.lead(4)["telefon"], {"ergebnis": "termin_verschoben"}, direction="inbound").json()
    assert r["ergebnis"] == "sonstiges" and "termin_verschoben_behauptet_ohne_beleg" in r["flags"]


def test_verification_needs_a_call_id_and_a_stored_plz(inbound):
    app, client, sim = inbound
    crm = app.state.ctx.crm
    r = sim.tool("verify_customer", None, "1", plz=crm.lead(1)["plz"])
    assert r["ok"] is False and r["weiterleiten"] is True and "call.id" in r["fehler"]
    assert crm.verifizierungsversuche("ohne_call_id", 1) == [] and crm.con.execute("SELECT COUNT(*) FROM verifizierungen").fetchone()[0] == 0
    crm.con.execute("UPDATE leads SET plz = NULL WHERE id = 2")
    r = sim.tool("verify_customer", "call_x", "2", plz="48155")
    assert r["ok"] is False and r["weiterleiten"] is True and "nicht prüfbar" in r["fehler"]


def test_rebooking_honours_the_minimum_notice_too(inbound):
    app, client, sim = inbound
    crm = app.state.ctx.crm
    alt = termin_anlegen(crm, 2, in_stunden=12)
    sim.tool("verify_customer", "c2", "2", plz=crm.lead(2)["plz"])
    slots = sim.tool("check_slots", "c2", "2")["freie_termine"]
    r = sim.tool("book_appointment", "c2", "2", slot_id=slots[0]["slot_id"])
    assert r["ok"] is False and r["weiterleiten"] is True
    assert crm.termin(2)["slot_id"] == alt and crm.stornierungen(2) == []


def test_ticket_and_cancel_retries_are_idempotent(inbound, kundensystem):
    app, client, sim = inbound
    crm = app.state.ctx.crm
    termin_anlegen(crm, 3)
    sim.tool("verify_customer", "c3", "3", plz=crm.lead(3)["plz"])
    erstes = sim.tool("create_ticket", "c3", "3", kategorie="stoerung_tv", beschreibung="Kein Bild")
    zweites = sim.tool("create_ticket", "c3", "3", kategorie="stoerung_tv", beschreibung="Kein Bild")      # Retry nach Timeout
    assert erstes["ticket_nummer"] == zweites["ticket_nummer"] == "T-00001" and "bereits angelegt" in zweites["hinweis"]
    assert len(crm.tickets(3)) == 1
    bananen = sim.tool("create_ticket", "c3", "3", kategorie="bananen", beschreibung="Router piept")
    assert bananen["kategorie"] == "sonstiges" and "unbekannt" in bananen["hinweis"]
    a1 = sim.tool("cancel_appointment", "c3", "3", grund="Urlaub")
    a2 = sim.tool("cancel_appointment", "c3", "3", grund="Urlaub")
    assert a1["ok"] and a2["ok"] and a2["abgesagt"] == a1["abgesagt"] and "bereits abgesagt" in a2["hinweis"]
    assert len(crm.stornierungen(3)) == 1
    app.state.ctx.planer.tick()
    assert [e["typ"] for e in kundensystem["empfangen"]].count("ticket.erstellt") == 2        # zwei verschiedene Tickets
    assert [e["typ"] for e in kundensystem["empfangen"]].count("termin.abgesagt") == 1


def test_ticket_plus_rebooking_in_one_call_keeps_the_booking_in_the_event_stream(inbound, kundensystem):
    app, client, sim = inbound
    crm = app.state.ctx.crm
    termin_anlegen(crm, 1)
    call_id, tel = "c_mix", crm.lead(1)["telefon"]
    sim.tool("verify_customer", call_id, "1", plz=crm.lead(1)["plz"])
    sim.tool("create_ticket", call_id, "1", kategorie="stoerung_internet", beschreibung="Kein Netz")
    sim.tool("cancel_appointment", call_id, "1", grund="Techniker soll erst nach der Entstörung kommen")
    slots = sim.tool("check_slots", call_id, "1")["freie_termine"]
    sim.tool("book_appointment", call_id, "1", slot_id=slots[0]["slot_id"])
    r = sim.call_ended(call_id, "1", tel, {"ergebnis": "termin_verschoben"}, direction="inbound").json()
    assert r["ergebnis"] == "ticket_erstellt" and r["termin"].endswith("Z")
    app.state.ctx.planer.tick()
    typen = [e["typ"] for e in kundensystem["empfangen"]]
    assert "termin.gebucht" in typen and "ticket.erstellt" in typen and "termin.abgesagt" in typen
    gebucht = next(e for e in kundensystem["empfangen"] if e["typ"] == "termin.gebucht")
    assert gebucht["body"]["daten"]["umbuchung"] is True


def test_direct_rebooking_emits_the_cancellation_of_the_old_slot(sim, app, kundensystem):
    """PV-Fall: ein zweiter Slot ohne vorherige Absage ist eine Umbuchung, das Kundensystem erfährt beides."""
    crm = app.state.ctx.crm
    sim.tool("update_lead", "c", "1", status="qualifiziert", eigentuemer=True, gebaeudetyp="einfamilienhaus")
    slots = sim.tool("check_slots", "c", "1")["freie_termine"]
    sim.tool("book_appointment", "c", "1", slot_id=slots[0]["slot_id"])
    r = sim.tool("book_appointment", "c", "1", slot_id=slots[1]["slot_id"])
    assert r["umgebucht_von"] and crm.stornierungen(1)[0]["grund"] == "Umbuchung" and crm.stornierungen(1)[0]["call_id"] == "c"
    app.state.ctx.planer.tick()
    abgesagt = next(e for e in kundensystem["empfangen"] if e["typ"] == "termin.abgesagt")
    assert abgesagt["body"]["daten"]["umbuchung"] is True and abgesagt["body"]["daten"]["abgesagt"] == r["umgebucht_von"]
    ce = sim.call_ended("c", "1", crm.lead(1)["telefon"], {"ergebnis": "termin_gebucht"}).json()
    assert ce["ergebnis"] == "termin_verschoben"


def test_unknown_inbound_caller_is_captured_as_a_lead_with_a_callback_task(inbound):
    app, client, sim = inbound
    crm = app.state.ctx.crm
    assert sim.contact_lookup("+49 170 5550001") == {"contact": None}
    r = sim.call_ended("c_unbekannt", None, "+49 170 5550001", {"ergebnis": "rueckruf_vereinbart"}, direction="inbound").json()
    assert r["status"] == "verarbeitet" and r["ergebnis"] == "rueckruf_vereinbart"
    lead = crm.lead_per_telefon("+491705550001")
    assert lead["quelle"] == "Inbound-Anruf" and lead["status"] == "rueckruf" and crm.aufgaben(lead["id"])[0]["titel"] == "Rückruf"
    assert sim.contact_lookup("+49 170 5550001")["contact"]["external_id"] == str(lead["id"])
