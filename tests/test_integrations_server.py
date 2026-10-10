"""HTTP-Kontrakte gegenüber der Plattform: Custom Tools, Custom Calendar, Contact Lookup, Webhook-Eingang.

Der Simulator schickt die Requests so, wie telli sie dokumentiert. Geprüft wird die Antwortform
(Feldnamen, Zeitformate, Statuscodes), die Authentifizierung und dass jede Geschäftsregel aus
agent.Tools auch über HTTP gilt.
"""
import json
import time

import pytest

from conftest import API_KEY, TOOL_SECRET
from integrations.modelle import iso_lesen
from integrations.signatur import telli_signatur


BETRIEB = {"Authorization": f"Bearer {TOOL_SECRET}"}


def lead(app, lead_id):
    return app.state.ctx.crm.lead(lead_id)


# ---------------------------------------------------------------- Custom Tools
def test_tools_need_the_bearer_token_and_a_known_name(client):
    assert client.post("/tools/update_lead", json={"external_id": "1", "status": "in_qualifizierung"}).status_code == 401
    assert client.post("/tools/end_call", json={"external_id": "1"}).status_code == 401              # erst Auth, dann 404
    kopf = {"Authorization": f"Bearer {TOOL_SECRET}"}
    assert client.post("/tools/end_call", json={"external_id": "1"}, headers=kopf).status_code == 404
    assert client.post("/tools/update_lead", content=b"{kaputt", headers=kopf).status_code == 400
    r = client.post("/tools/update_lead", json={"external_id": "77", "status": "in_qualifizierung"}, headers=kopf)
    assert r.status_code == 200 and r.json()["ok"] is False and "nicht im CRM" in r.json()["fehler"]


def test_tool_errors_come_back_as_200_so_the_agent_can_react(sim, app):
    r = sim.tool("book_appointment", "call_1", "1", slot_id=1)
    assert r["ok"] is False and "qualifizierter Eigentümer" in r["fehler"]
    r = sim.tool("update_lead", "call_1", "1", status="in_qualifizierung", dachflaeche_m2=99999)
    assert r["ok"] is False and r["validierungsfehler"]
    assert sim.protokoll[-1]["status"] == 200


def test_tools_resolve_the_lead_by_external_id_or_phone(sim, app):
    assert sim.tool("lookup_lead", "call_1", "3")["name"] == "Mehmet Yilmaz"
    per_telefon = sim.tool("lookup_lead", "call_1", None, telefon="0251 000004")
    assert per_telefon["ok"] is True and per_telefon["name"] == "Anna Schröder" and per_telefon["gesperrt"] is False
    assert sim.tool("lookup_lead", "call_1", "999", telefon="+49 251 000005")["name"] == "Klaus Hoffmann"


def test_business_rules_hold_across_separate_tool_requests(sim, app):
    sim.tool("update_lead", "call_1", "1", status="in_qualifizierung", eigentuemer=True, gebaeudetyp="reihenhaus")
    assert sim.tool("check_slots", "call_1", "1")["ok"] is False                      # noch nicht qualifiziert
    assert sim.tool("update_lead", "call_1", "1", status="qualifiziert")["ok"] is True
    slots = sim.tool("check_slots", "call_1", "1")["freie_termine"]
    assert len(slots) == 2
    assert sim.tool("book_appointment", "call_1", "1", slot_id=slots[1]["slot_id"] + 5)["ok"] is False   # nie angeboten
    r = sim.tool("book_appointment", "call_1", "1", slot_id=slots[1]["slot_id"])
    assert r["ok"] is True and "termin" in r
    assert app.state.ctx.crm.termin(1)["slot_id"] == slots[1]["slot_id"]


def test_tool_definitions_match_the_http_tools(client):
    assert client.get("/tools/definitions").status_code == 401
    d = client.get("/tools/definitions", headers=BETRIEB).json()
    namen = [t["name"] for t in d["telli_custom_tools"]]
    assert namen == ["lookup_lead", "update_lead", "check_slots", "book_appointment", "cancel_appointment"]   # Reihenfolge des Use Case
    assert "verify_customer" not in namen                                                                   # gehört zum Inbound-Fall
    update = next(t for t in d["telli_custom_tools"] if t["name"] == "update_lead")
    assert update["method"] == "POST" and update["url"].endswith("/tools/update_lead")
    assert update["headers"][0]["value_type"] == "Secret"
    system = {b["key"]: b["value"] for b in update["body"] if b["value_type"] == "System Variable"}
    assert system == {"call_id": "call.id", "external_id": "contact.externalId", "phone_number": "contact.phoneNumber"}
    status = next(b for b in update["body"] if b["key"] == "status")
    assert status["value_type"] == "LLM Parameter" and status["required"] and "qualifiziert" in status["description"]
    assert next(b for b in update["body"] if b["key"] == "eigentuemer")["data_type"] == "Boolean"
    assert [t["function"]["name"] for t in d["openai_tools"]] == namen


# ---------------------------------------------------------------- Custom Calendar
def test_calendar_requires_a_valid_telli_signature(client):
    body = json.dumps({"contact": {"externalId": "1"}}).encode()
    assert client.post("/calendar/available", content=body).status_code == 401
    falsch = {"x-telli-signature": telli_signatur("anderer-key", body), "Content-Type": "application/json"}
    assert client.post("/calendar/available", content=body, headers=falsch).status_code == 401
    richtig = {"x-telli-signature": telli_signatur(API_KEY, body), "Content-Type": "application/json"}
    assert client.post("/calendar/available", content=body, headers=richtig).status_code == 200


def test_available_follows_the_contract_and_the_qualification_rule(sim, app):
    assert sim.available("1", "+49 251 000001") == {"available": []}                 # nicht qualifiziert: leer, nichts extra
    sim.tool("update_lead", "c", "1", status="qualifiziert", eigentuemer=True, gebaeudetyp="einfamilienhaus")
    r = sim.available("1", "+49 251 000001")
    assert set(r) == {"available"} and len(r["available"]) == 4
    erster = r["available"][0]
    assert set(erster) == {"start_iso", "end_iso"}
    assert erster["start_iso"].endswith(".000Z") and iso_lesen(erster["end_iso"]) > iso_lesen(erster["start_iso"])
    assert sim.available("99", "+49 999")["available"] == []


def test_book_is_idempotent_and_reports_failures_in_the_body(sim, app):
    sim.tool("update_lead", "c", "1", status="qualifiziert", eigentuemer=True, gebaeudetyp="einfamilienhaus")
    slots = sim.available("1", "+49 251 000001")["available"]
    assert sim.book("1", "+49 251 000001", "2030-01-01T10:00:00.000") == {
        "status": "failed", "reason": "Appointment slot is no longer available"}
    assert sim.book("1", "+49 251 000001", "kein datum")["status"] == "failed"
    assert sim.book("1", "+49 251 000001", slots[0]["start_iso"]) == {"status": "success"}
    assert sim.book("1", "+49 251 000001", slots[0]["start_iso"]) == {"status": "success"}      # Wiederholung
    assert app.state.ctx.crm.con.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 1
    sim.tool("update_lead", "c", "2", status="qualifiziert", eigentuemer=True, gebaeudetyp="reihenhaus")
    nie_angeboten = sim.book("2", "+49 251 000002", slots[1]["start_iso"])
    assert nie_angeboten["status"] == "failed" and "nicht angeboten" in nie_angeboten["reason"]
    assert sim.book("2", "+49 251 000002", slots[0]["start_iso"]) == {
        "status": "failed", "reason": "Appointment slot is no longer available"}                       # von Lead 1 belegt
    sim.available("2", "+49 251 000002")
    assert sim.book("2", "+49 251 000002", slots[1]["start_iso"]) == {"status": "success"}
    nicht_qualifiziert = sim.book("3", "+49 251 000003", slots[2]["start_iso"])
    assert nicht_qualifiziert["status"] == "failed" and "qualifizierter Eigentümer" in nicht_qualifiziert["reason"]
    assert sim.protokoll[-1]["status"] == 200


# ---------------------------------------------------------------- Contact Lookup
def test_contact_lookup_answers_known_and_unknown_callers(sim, app, client):
    r = sim.contact_lookup("0251 000002")
    assert r["contact"]["first_name"] == "Sabine" and r["contact"]["last_name"] == "Wolf"
    assert r["contact"]["external_id"] == "2" and r["contact"]["properties"]["status"] == "offen"
    assert r["contact"]["properties"]["gesperrt"] is False and "email" not in r["contact"]
    assert sim.contact_lookup("+49 170 0000000") == {"contact": None}
    body = json.dumps({"event": "contact_lookup"}).encode()
    kopf = {"x-telli-signature": telli_signatur(API_KEY, body), "Content-Type": "application/json"}
    assert client.post("/webhooks/contact-lookup", content=body, headers=kopf).status_code == 400


# ---------------------------------------------------------------- Webhook-Eingang
def test_webhook_rejects_bad_signatures_and_ignores_other_events(sim, client):
    r = sim.call_ended("call_x", "1", "+49 251 000001", {"ergebnis": "opt_out"}, zeitstempel=1_000_000)
    assert r.status_code == 401                                                        # Zeitstempel zu alt
    body = json.dumps({"event": "call_rescheduled"}).encode()
    assert client.post("/webhooks/telli", content=body).status_code == 401              # unsigniert
    from integrations.signatur import svix_kopfzeilen
    from conftest import WEBHOOK_SECRET
    kopf = svix_kopfzeilen(WEBHOOK_SECRET, "msg_r", body)
    assert client.post("/webhooks/telli", content=body, headers=kopf).json()["status"] == "lead_unbekannt"
    body = json.dumps({"event": "contact_status_changed"}).encode()
    kopf = svix_kopfzeilen(WEBHOOK_SECRET, "msg_c", body)
    assert client.post("/webhooks/telli", content=body, headers=kopf).json() == {"status": "ignoriert", "event": "contact_status_changed"}


def test_health_and_metrics(client, sim):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["geheimnisse_vollstaendig"] is True and "fehlende_geheimnisse" not in h
    sim.tool("lookup_lead", "c", "1")
    assert client.get("/metrics").status_code == 401
    m = client.get("/metrics", headers=BETRIEB).json()
    assert m["endpunkte"]["tools/lookup_lead"]["anzahl"] == 1 and m["outbox"] == {} and m["fehlende_geheimnisse"] == []
    assert client.get("/outbox").status_code == 401 and client.post("/outbox/zustellen").status_code == 401
    assert client.get("/outbox", headers=BETRIEB).json() == {"konfiguriert": True, "status": {}, "tot": []}


def test_push_endpoint_runs_the_lead_push_immediately(client, app, telli_attrappe):
    assert client.post("/push").status_code == 401
    bericht = client.post("/push", headers=BETRIEB).json()["push"]
    assert [a["lead_id"] for a in bericht["angelegt"]] == [1, 2, 3, 4, 5] and len(bericht["geplant"]) == 5
    assert [r[1] for r in telli_attrappe["requests"]].count("/v2/contacts") == 5
    assert app.state.ctx.crm.telli_kontakt(1)["loop_id"].startswith("loop_")
    assert client.post("/push", headers=BETRIEB).json()["push"]["angelegt"] == []            # idempotent


def test_tool_booking_retry_is_idempotent_and_a_second_slot_is_a_rebooking(sim, app):
    crm = app.state.ctx.crm
    sim.tool("update_lead", "c", "1", status="qualifiziert", eigentuemer=True, gebaeudetyp="einfamilienhaus")
    slots = sim.tool("check_slots", "c", "1")["freie_termine"]
    erste = sim.tool("book_appointment", "c", "1", slot_id=slots[0]["slot_id"])
    wiederholt = sim.tool("book_appointment", "c", "1", slot_id=slots[0]["slot_id"])            # Retry nach Timeout
    assert erste["ok"] and wiederholt["ok"] and wiederholt["termin"] == erste["termin"]
    assert crm.con.execute("SELECT COUNT(*) FROM appointments WHERE lead_id = 1").fetchone()[0] == 1
    umbuchung = sim.tool("book_appointment", "c", "1", slot_id=slots[1]["slot_id"])
    assert umbuchung["ok"] and umbuchung["umgebucht_von"] == erste["termin"]
    assert crm.con.execute("SELECT COUNT(*) FROM appointments WHERE lead_id = 1").fetchone()[0] == 1
    assert crm.con.execute("SELECT lead_id FROM slots WHERE id = ?", [slots[0]["slot_id"]]).fetchone()[0] is None
    assert crm.termin(1)["slot_id"] == slots[1]["slot_id"]


def test_garbage_auth_headers_are_401_not_500(client):
    for pfad, kopf in [("/tools/lookup_lead", {b"authorization": b"Bearer \xe4\xf6\xfc"}),
                       ("/calendar/available", {b"x-telli-signature": b"\xff\xfe"}),
                       ("/webhooks/telli", {b"svix-id": b"m", b"svix-timestamp": b"1", b"svix-signature": b"v1,\xe9"}),
                       ("/webhooks/contact-lookup", {b"x-telli-signature": b"falsch"})]:
        r = client.post(pfad, content=b"{}", headers={b"content-type": b"application/json", **kopf})
        assert r.status_code == 401, pfad


def test_facts_from_an_inbound_call_resolve_the_lead_by_from_number(sim, app):
    """Rückruf des Kunden: external_contact_id fehlt, die Nummer steht in from_number."""
    sim.call_ended("call_in", None, "+49 251 000003", {"ergebnis": "kein_interesse"})
    assert app.state.ctx.crm.lead(3)["status"] == "kein_interesse"


def test_flat_call_ended_payload_without_event_key(client):
    from conftest import WEBHOOK_SECRET
    from integrations.signatur import svix_kopfzeilen
    body = json.dumps({"call_id": "flach_1", "external_contact_id": "4", "status": "connected", "state": "ended",
                       "outcomes": [{"key": "ergebnis", "value": "kein_interesse"}]}).encode()
    r = client.post("/webhooks/telli", content=body, headers=svix_kopfzeilen(WEBHOOK_SECRET, "msg_flach", body))
    assert r.json()["status"] == "verarbeitet" and r.json()["ergebnis"] == "kein_interesse"


def test_hubspot_mirror_and_lead_push_are_wired_from_config(con, konfig, quiet):
    from integrations.hubspot import HubSpotCRM
    from integrations.server import erstelle_app
    konfig.hubspot_token = "pat-x"
    konfig.telli_api_key = "alt,neu"                                                   # Rotation: Client nimmt den neuesten
    app = erstelle_app(konfig, con=con, planer_starten=False)
    assert isinstance(app.state.ctx.nachbereitung.spiegel, HubSpotCRM)
    assert app.state.ctx.lead_push is not None and app.state.ctx.lead_push.agent_id == "agent_test"
    assert app.state.ctx.telli_client.client.headers["Authorization"] == "Bearer neu"
    assert app.state.ctx.abgleich is not None


def test_prod_environment_refuses_to_start_without_secrets(con):
    from integrations.konfig import Konfig
    from integrations.server import erstelle_app
    with pytest.raises(RuntimeError, match="TELLI_API_KEY"):
        erstelle_app(Konfig(umgebung="prod"), con=con, planer_starten=False)


def test_explicit_push_and_delivery_wait_for_a_running_tick_instead_of_being_dropped(app, client, telli_attrappe, kundensystem):
    """N1/N2: /push und /outbox/zustellen laufen über den Planer und warten auf einen laufenden Durchlauf."""
    import threading
    planer = app.state.ctx.planer
    freigabe = threading.Event()
    original = app.state.ctx.outbox.zustellen

    def langsame_zustellung(**kw):
        freigabe.wait(5)
        return original(**kw)

    app.state.ctx.outbox.zustellen = langsame_zustellung
    planer.letzter_push = time.monotonic()                                      # Hintergrund-Tick ohne Push
    laufend = threading.Thread(target=planer.tick)
    laufend.start()
    time.sleep(0.2)
    assert planer.tick() == {"uebersprungen": "läuft bereits"}                 # Hintergrund: überspringen
    ergebnis = {}
    anfrage = threading.Thread(target=lambda: ergebnis.update(client.post("/push", headers=BETRIEB).json()))
    anfrage.start()
    time.sleep(0.2)
    assert not ergebnis                                                         # wartet, statt abzubrechen
    freigabe.set()
    anfrage.join(5); laufend.join(5)
    assert len(ergebnis["push"]["angelegt"]) == 5
    assert client.get("/metrics", headers=BETRIEB).json()["push"]["angelegt"][0]["lead_id"] == 1


def test_a_failing_planer_step_does_not_stop_the_others(app, telli_attrappe):
    planer = app.state.ctx.planer
    app.state.ctx.outbox.zustellen = lambda **kw: (_ for _ in ()).throw(RuntimeError("Outbox kaputt"))
    bericht = planer.tick(push=True)
    assert bericht["outbox"] == {"fehler": "RuntimeError: Outbox kaputt"} and len(bericht["push"]["angelegt"]) == 5


def test_cancel_tool_releases_the_slot_and_emits_an_event(sim, app, kundensystem):
    crm = app.state.ctx.crm
    assert sim.tool("cancel_appointment", "c", "1")["ok"] is False                       # nichts zu stornieren
    sim.tool("update_lead", "c", "1", status="qualifiziert", eigentuemer=True, gebaeudetyp="einfamilienhaus")
    slots = sim.tool("check_slots", "c", "1")["freie_termine"]
    sim.tool("book_appointment", "c", "1", slot_id=slots[0]["slot_id"])
    assert sim.tool("lookup_lead", "c", "1")["naechster_termin"].endswith("Uhr")
    r = sim.tool("cancel_appointment", "c", "1", grund="Bin im Urlaub")
    assert r["ok"] and r["grund"] == "Bin im Urlaub"
    assert crm.termin(1) is None and crm.stornierungen(1)[0]["grund"] == "Bin im Urlaub"
    assert crm.con.execute("SELECT lead_id FROM slots WHERE id = ?", [slots[0]["slot_id"]]).fetchone()[0] is None
    assert crm.erinnerungen(1)[0]["status"] == "entfallen"
    app.state.ctx.planer.tick()
    assert [e["typ"] for e in kundensystem["empfangen"]] == ["termin.abgesagt"]
    assert sim.tool("lookup_lead", "c", "1")["naechster_termin"] is None
    assert sim.tool("book_appointment", "c", "1", slot_id=slots[0]["slot_id"])["ok"]       # Slot wieder buchbar


def test_reminder_is_sent_24h_before_the_appointment(sim, app, kundensystem):
    crm = app.state.ctx.crm
    sim.tool("update_lead", "c", "1", status="qualifiziert", eigentuemer=True, gebaeudetyp="einfamilienhaus")
    slots = sim.tool("check_slots", "c", "1")["freie_termine"]
    sim.tool("book_appointment", "c", "1", slot_id=slots[0]["slot_id"])
    erinnerung = crm.erinnerungen(1)[0]
    assert erinnerung["status"] == "offen" and erinnerung["faellig_am"] == crm.termin(1)["start"] - __import__("datetime").timedelta(hours=24)
    assert app.state.ctx.planer.tick()["erinnerungen"] == {"gesendet": 0}                  # noch nicht fällig
    crm.con.execute("UPDATE erinnerungen SET faellig_am = now()::TIMESTAMP - INTERVAL 1 MINUTE")
    assert app.state.ctx.planer.tick()["erinnerungen"] == {"gesendet": 1}
    assert app.state.ctx.planer.tick()["erinnerungen"] == {"gesendet": 0}                  # genau einmal
    ereignis = next(e for e in kundensystem["empfangen"] if e["typ"] == "termin.erinnerung")
    assert ereignis["body"]["daten"]["name"] == "Thomas Becker" and ereignis["body"]["daten"]["start"].endswith("Z")
    sim.tool("book_appointment", "c", "1", slot_id=slots[1]["slot_id"])                     # Umbuchung: neue Erinnerung
    assert [e["status"] for e in crm.erinnerungen(1)] == ["gesendet", "offen"]


def _signiert(body: dict, msg_id: str):
    from conftest import WEBHOOK_SECRET
    from integrations.signatur import svix_kopfzeilen
    roh = json.dumps(body).encode()
    return roh, svix_kopfzeilen(WEBHOOK_SECRET, msg_id, roh)


def test_call_rescheduled_stores_the_next_attempt(client, app, kundensystem):
    crm = app.state.ctx.crm
    crm.telli_kontakt_speichern(2, "ct_2", "loop_2")
    roh, kopf = _signiert({"event": "call_rescheduled",
                           "call": {"loop_id": "loop_2", "contact_id": "ct_2", "agent_id": "agent_test",
                                    "to_number": "+49251000002", "next_call_at": "2026-10-13T08:00:00.000Z"},
                           "contact": {"contact_id": "ct_2", "next_call_at": "2026-10-13T08:00:00.000Z"}}, "msg_rs1")
    r = client.post("/webhooks/telli", content=roh, headers=kopf).json()
    assert r["status"] == "verarbeitet" and r["lead_id"] == 2 and r["naechster_anruf"] == "2026-10-13T08:00:00.000Z"
    assert crm.telli_kontakt(2)["next_call_at"] == __import__("datetime").datetime(2026, 10, 13, 10, 0)     # Wanduhrzeit Berlin
    assert "Nächster Anrufversuch" in crm.notizen(2)[0]
    assert client.post("/webhooks/telli", content=roh, headers=kopf).json()["status"] == "duplikat"
    assert [e["typ"] for e in kundensystem["empfangen"]] == ["anruf.geplant"]
    # nur Telefonnummer bekannt (Kontakt nicht aus dem Lead-Push): Auflösung über die Nummer
    roh, kopf = _signiert({"event": "call_rescheduled", "call": {"loop_id": "loop_x", "to_number": "+49 251 000003",
                                                                 "next_call_at": "2026-10-14T08:00:00.000Z"}}, "msg_rs2")
    assert client.post("/webhooks/telli", content=roh, headers=kopf).json()["lead_id"] == 3
    k = crm.telli_kontakt(3)
    assert k["next_call_at"] == __import__("datetime").datetime(2026, 10, 14, 10, 0) and k["loop_id"] == "loop_x"   # Zeile wurde angelegt
    assert crm.leads_ohne_anrufplanung() == [] or all(l["id"] != 3 for l in crm.leads_ohne_anrufplanung())           # kein zweiter Loop
    roh, kopf = _signiert({"event": "call_rescheduled", "call": {"loop_id": "loop_x", "to_number": "+49 251 000003",
                                                                 "next_call_at": "2026-10-14T08:00:00.000Z"}}, "msg_rs3")
    assert client.post("/webhooks/telli", content=roh, headers=kopf).json()["status"] == "duplikat"               # Replay, neue ID
    assert len(crm.notizen(3)) == 1


def test_auto_dialer_gave_up_creates_a_follow_up_task(client, app, kundensystem):
    crm = app.state.ctx.crm
    crm.telli_kontakt_speichern(4, "ct_4", "loop_4")
    roh, kopf = _signiert({"event": "auto_dialer_status_changed", "auto_dialer": {"status": "in_dialer", "updated_at": "2026-10-10T08:00:00Z"},
                           "contact": {"contact_id": "ct_4"}}, "msg_ad1")
    r = client.post("/webhooks/telli", content=roh, headers=kopf).json()
    assert r["status"] == "verarbeitet" and r["dialer"] == "in_dialer" and crm.aufgaben(4) == []
    roh, kopf = _signiert({"event": "auto_dialer_status_changed",
                           "auto_dialer": {"status": "not_in_dialer", "reason": "scheduler_gave_up_smart"},
                           "contact": {"id": "ct_4", "externalId": "4"}}, "msg_ad2")
    r = client.post("/webhooks/telli", content=roh, headers=kopf).json()
    assert r["aufgabe_id"] == 1 and crm.aufgaben(4)[0]["titel"].startswith("Nicht erreicht")
    assert crm.telli_kontakt(4)["dialer_status"] == "not_in_dialer" and crm.telli_kontakt(4)["dialer_grund"] == "scheduler_gave_up_smart"
    assert [e["typ"] for e in kundensystem["empfangen"]] == ["aufgabe.erstellt", "dialer.beendet"]
    roh, kopf = _signiert({"event": "auto_dialer_status_changed",
                           "auto_dialer": {"status": "not_in_dialer", "reason": "last_call_was_connected"},
                           "contact": {"contact_id": "ct_4"}}, "msg_ad3")
    assert "aufgabe_id" not in client.post("/webhooks/telli", content=roh, headers=kopf).json()      # Erfolg: keine Aufgabe
    assert len(crm.aufgaben(4)) == 1


def _telli_call(call_id, contact_id, ergebnis="kein_interesse", state="ended", status="connected"):
    return {"call_id": call_id, "contact_id": contact_id, "attempt": 1, "direction": "outbound", "to_number": "+49251000001",
            "state": state, "status": status, "ended_reason": "agent-ended-call",
            "started_at_iso": "2026-10-10T09:00:00.000Z", "ended_at_iso": "2026-10-10T09:03:00.000Z",
            "transcriptObject": [{"role": "agent", "content": "Guten Tag"}, {"role": "user", "content": "Nein danke"}],
            "outcomes": [{"key": "ergebnis", "dataType": "category", "value": ergebnis}], "collected_data": None, "appointments": []}


def test_reconciliation_catches_calls_whose_webhook_never_arrived(client, app, telli_attrappe, kundensystem):
    crm, planer = app.state.ctx.crm, app.state.ctx.planer
    client.post("/push", headers=BETRIEB)                                             # Kontakte ct_1..ct_5 existieren
    telli_attrappe["anrufe"]["ct_1"] = [_telli_call("call_a", "ct_1", "kein_interesse"),
                                        _telli_call("call_laeuft", "ct_1", state="in_progress", status=None)]
    telli_attrappe["anrufe"]["ct_2"] = [_telli_call("call_b", "ct_2", "opt_out")]
    assert "abgleich" not in planer.tick()                                               # nicht fällig: kein Abgleich im Takt
    assert client.post("/abgleich").status_code == 401
    bericht = client.post("/abgleich", headers=BETRIEB).json()
    assert bericht["kontakte"] == 5 and bericht["offen"] == 1
    assert [(n["lead_id"], n["call_id"], n["ergebnis"]) for n in bericht["nachgeholt"]] == [(1, "call_a", "kein_interesse"), (2, "call_b", "opt_out")]
    assert crm.lead(1)["status"] == "kein_interesse" and crm.lead(2)["status"] == "opt_out" and crm.gesperrt(crm.lead(2)["telefon"])
    assert crm.anrufe(1)[0]["ergebnis"] == "kein_interesse"
    # trifft der Webhook doch noch ein, passiert nichts doppelt
    roh, kopf = _signiert({"event": "call_ended", "call": _telli_call("call_a", "ct_1"), "contact": {"external_contact_id": "1"}}, "msg_spaet")
    assert client.post("/webhooks/telli", content=roh, headers=kopf).json()["status"] == "duplikat"
    assert len(crm.anrufe(1)) == 1
    # zweiter Abgleich kurz danach: Kontakte sind frisch abgeglichen, nichts zu tun; nach Ablauf erneut, alles bekannt
    assert client.post("/abgleich", headers=BETRIEB).json()["kontakte"] == 0
    crm.con.execute("UPDATE telli_kontakte SET abgleich_am = abgleich_am - INTERVAL 2 HOUR")
    b = client.post("/abgleich", headers=BETRIEB).json()
    assert b["kontakte"] == 5 and b["bekannt"] == 1 + 1 and b["nachgeholt"] == []
    assert client.get("/metrics", headers=BETRIEB).json()["abgleich"]["kontakte"] == 5


def test_single_call_can_be_fetched_and_processed_after_an_outage(client, app, telli_attrappe):
    crm = app.state.ctx.crm
    crm.telli_kontakt_speichern(3, "ct_3", "loop_3")
    telli_attrappe["anrufe"]["ct_3"] = [_telli_call("call_c", "ct_3", "rueckruf_vereinbart")]
    r = client.post("/abgleich/call_c", headers=BETRIEB).json()
    assert r["status"] == "verarbeitet" and r["lead_id"] == 3 and crm.lead(3)["status"] == "rueckruf"
    assert client.post("/abgleich/call_c", headers=BETRIEB).json()["status"] == "duplikat"
    r = client.post("/abgleich/gibt_es_nicht", headers=BETRIEB)
    assert r.status_code == 404 and "Call not found" in r.json()["detail"]
