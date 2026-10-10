"""Lokale Integrationsdatenbank und HubSpot-Adapter.

LocalCRM hält den operativen Zustand (Angebote, Buchungen, Aufgaben, Sperrliste) und schreibt
nur über agent.Tools, damit keine Geschäftsregel umgangen wird. Der HubSpot-Adapter wird gegen
die dokumentierte API-Form geprüft: Pfad, Methode, Body, Assoziationstyp.
"""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import agent
from integrations.crm import LocalCRM, PersistenteTools
from integrations.hubspot import NOTE_TO_CONTACT, TASK_TO_CONTACT, Feldmapping, HubSpotCRM, HubSpotFehler
from integrations.modelle import telefon_normalisieren


@pytest.fixture
def crm(con):
    return LocalCRM(con)


def test_phone_numbers_are_normalised_for_lookup(crm):
    assert telefon_normalisieren("+49 251 000001") == "+49251000001"
    assert telefon_normalisieren("0251 000001") == "+49251000001"
    assert telefon_normalisieren("0049 251 000001") == "+49251000001"
    assert crm.lead_per_telefon("+49251000001")["name"] == "Thomas Becker"
    assert crm.lead_per_telefon("0251000001")["id"] == 1
    assert crm.lead_per_telefon("+49 999") is None
    assert crm.lead("abc") is None and crm.lead(99) is None


def test_offered_slots_survive_a_new_request(crm):
    """Jeder Tool-Aufruf ist ein eigener HTTP-Request: Das Angebotsgedächtnis muss in der DB liegen."""
    tools = crm.tools(1)
    tools.update_lead(status="qualifiziert", eigentuemer=True, gebaeudetyp="einfamilienhaus")
    angeboten = tools.check_slots()["freie_termine"]
    slot = angeboten[0]["slot_id"]
    frisch = crm.tools(1)                                   # neuer Request, neues Tools-Objekt
    assert isinstance(frisch, PersistenteTools) and slot in frisch.offered
    assert frisch.book_appointment(slot_id=slot)["ok"] is True
    assert agent.Tools(crm.con, 1).book_appointment(slot_id=angeboten[1]["slot_id"])["ok"] is False   # ohne Gedächtnis: nie angeboten


def test_offers_expire(crm):
    crm.con.execute("INSERT INTO angebote VALUES (1, 1, now() - INTERVAL 25 HOUR)")
    crm.con.execute("INSERT INTO angebote VALUES (1, 2, now() - INTERVAL 1 HOUR)")
    assert crm.angebotene_slots(1) == {2}


def test_update_without_status_keeps_business_rules(crm):
    assert crm.lead_aktualisieren(1, eigentuemer=True)["ok"] is True
    assert crm.lead(1)["status"] == "in_qualifizierung"
    assert crm.lead_aktualisieren(1, status="qualifiziert")["ok"] is False     # Gebäudetyp fehlt
    assert crm.lead_aktualisieren(1, dachflaeche_m2=99999)["ok"] is False      # Validierung greift
    assert crm.lead_aktualisieren(42, eigentuemer=True)["ok"] is False


def test_slot_lookup_by_utc_start_and_booking(crm):
    slot = crm.freie_slots(1)[0]
    utc = crm.start_utc(slot["start"])
    assert utc.tzinfo is timezone.utc
    gefunden = crm.slot_per_start(utc)
    assert gefunden["slot_id"] == slot["slot_id"] and gefunden["lead_id"] is None
    assert crm.slot_per_start(utc + timedelta(minutes=7)) is None
    assert crm.termin(1) is None


def test_notes_tasks_blocklist_and_push_candidates(crm):
    crm.notiz(1, "Erste Notiz")
    crm.aufgabe(1, "Rückruf", datetime(2030, 1, 1, 10, tzinfo=timezone.utc), "morgen")
    assert crm.notizen(1) == ["Erste Notiz"]
    assert crm.aufgaben(1)[0]["titel"] == "Rückruf" and crm.aufgaben(1)[0]["status"] == "offen"
    crm.sperren("+49 251 000002", "Opt-out")
    crm.sperren("+49 251 000002", "Opt-out erneut")                 # idempotent
    assert crm.gesperrt("0251000002") is True and crm.gesperrt("+49 251 000003") is False
    kandidaten = [l["id"] for l in crm.leads_fuer_push()]
    assert kandidaten == [1, 3, 4, 5]                               # Lead 2 gesperrt
    crm.telli_kontakt_speichern(1, "ct_1")
    assert [l["id"] for l in crm.leads_fuer_push()] == [3, 4, 5]    # Lead 1 schon bei telli
    crm.telli_kontakt_speichern(1, "ct_1", "loop_9")
    assert crm.telli_kontakt(1) == {"contact_id": "ct_1", "loop_id": "loop_9"}


def test_webhook_deduplication_by_message_and_call(crm):
    assert crm.webhook_bekannt("msg_1", "call_1") is False
    crm.webhook_merken("msg_1", "call_1", "call_ended")
    assert crm.webhook_bekannt("msg_1") is True                      # Wiederholung durch die Plattform
    assert crm.webhook_bekannt("msg_2", "call_1") is True            # manuelles Replay mit neuer ID
    assert crm.webhook_bekannt("msg_2", "call_2") is False


def test_call_log_uses_the_agents_calls_table(crm):
    cid = crm.anruf_protokollieren(1, datetime.now(timezone.utc), datetime.now(timezone.utc), "opt_out", 2, 0,
                                   ["plattform:telli"], [{"role": "user", "content": "Nein danke"}])
    assert crm.anrufe(1) == [{"id": cid, "ergebnis": "opt_out", "turns": 2, "flags": ["plattform:telli"]}]
    assert crm.con.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 1


# ---------------------------------------------------------------- HubSpot
@pytest.fixture
def hubspot():
    """MockTransport: zeichnet Requests auf und antwortet wie die CRM-API v3."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, dict(request.url.params), body, dict(request.headers)))
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"total": 1, "results": [{"id": "501", "properties": {
                "firstname": "Thomas", "lastname": "Becker", "phone": "+49251000001", "zip": "48149",
                "hs_lead_status": "CONNECTED", "pv_eigentuemer": "true", "pv_dachflaeche_m2": "70.5",
                "pv_jahresverbrauch_kwh": "4200", "hs_analytics_source": "DIRECT_TRAFFIC"}}]})
        if request.url.path == "/crm/v3/objects/contacts/404":
            return httpx.Response(404, json={"status": "error"})
        if request.url.path == "/crm/v3/objects/contacts/500":
            return httpx.Response(500, text="kaputt")
        if request.method == "GET":
            return httpx.Response(200, json={"id": "501", "properties": {"firstname": "Thomas", "lastname": "Becker"}})
        return httpx.Response(201, json={"id": "9001"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.hubapi.com")
    crm = HubSpotCRM("pat-token", client=client)
    crm.requests = requests
    return crm


def test_hubspot_requests_follow_the_v3_api_shape(hubspot):
    lead = hubspot.lead_per_telefon("+49251000001")
    methode, pfad, _, body, kopf = hubspot.requests[-1]
    assert (methode, pfad) == ("POST", "/crm/v3/objects/contacts/search")
    assert body["filterGroups"][0]["filters"][0] == {"propertyName": "phone", "operator": "EQ", "value": "+49251000001"}
    assert kopf["authorization"] == "Bearer pat-token"
    assert lead["id"] == "501" and lead["name"] == "Thomas Becker" and lead["status"] == "qualifiziert"
    assert lead["eigentuemer"] is True and lead["dachflaeche_m2"] == 70.5 and lead["jahresverbrauch_kwh"] == 4200

    hubspot.lead_aktualisieren("501", status="opt_out", eigentuemer=False, gebaeudetyp="reihenhaus", notiz=None)
    methode, pfad, _, body, _ = hubspot.requests[-1]
    assert (methode, pfad) == ("PATCH", "/crm/v3/objects/contacts/501")
    assert body == {"properties": {"hs_lead_status": "UNQUALIFIED", "pv_opt_out": "true",
                                   "pv_eigentuemer": "false", "pv_gebaeudetyp": "reihenhaus"}}

    hubspot.notiz("501", "KI-Anruf: termin_gebucht")
    methode, pfad, _, body, _ = hubspot.requests[-1]
    assert (methode, pfad) == ("POST", "/crm/v3/objects/notes")
    assert body["properties"]["hs_note_body"] == "KI-Anruf: termin_gebucht" and body["properties"]["hs_timestamp"].endswith("Z")
    assert body["associations"][0]["to"] == {"id": "501"}
    assert body["associations"][0]["types"] == [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": NOTE_TO_CONTACT}]

    aid = hubspot.aufgabe("501", "Rückruf", datetime(2030, 1, 2, 9, tzinfo=timezone.utc), "Kunde will morgen")
    methode, pfad, _, body, _ = hubspot.requests[-1]
    assert (methode, pfad) == ("POST", "/crm/v3/objects/tasks") and aid == "9001"
    assert body["properties"]["hs_timestamp"] == "2030-01-02T09:00:00Z"
    assert body["properties"]["hs_task_status"] == "NOT_STARTED" and body["properties"]["hs_task_type"] == "CALL"
    assert body["associations"][0]["types"][0]["associationTypeId"] == TASK_TO_CONTACT


def test_hubspot_reads_requested_properties_and_handles_errors(hubspot):
    assert hubspot.lead("501")["name"] == "Thomas Becker"
    _, pfad, params, _, _ = hubspot.requests[-1]
    assert pfad == "/crm/v3/objects/contacts/501" and "pv_gebaeudetyp" in params["properties"].split(",")
    assert hubspot.lead("404") is None
    with pytest.raises(HubSpotFehler):
        hubspot.lead("500")
    assert hubspot.lead_aktualisieren("501", unbekanntes_feld=1) == {"ok": True, "gespeichert": {}}


def test_field_mapping_is_data_not_code():
    mapping = Feldmapping(eigenschaften={"status": "lead_status_custom"}, statuswerte={"qualifiziert": "SQL"})
    assert mapping.nach_hubspot({"status": "qualifiziert", "eigentuemer": True}) == {"lead_status_custom": "SQL"}
    assert mapping.aus_hubspot({"id": "7", "properties": {"hs_lead_status": "SQL"}})["status"] == "qualifiziert"
    assert mapping.aus_hubspot({"id": "7", "properties": {}})["status"] == "offen"


def test_contradicting_fact_resets_qualification_instead_of_being_dropped(crm):
    crm.lead_aktualisieren(1, status="qualifiziert", eigentuemer=True, gebaeudetyp="einfamilienhaus")
    r = crm.lead_aktualisieren(1, eigentuemer=False)
    assert r["ok"] is True and r["hinweis"] == "Qualifizierung zurückgesetzt"
    lead = crm.lead(1)
    assert lead["eigentuemer"] is False and lead["status"] == "in_qualifizierung"
    assert crm.lead_aktualisieren(1, dachflaeche_m2=99999)["ok"] is False             # echte Validierungsfehler bleiben Fehler


def test_timestamps_are_wall_clock_in_the_configured_zone(con):
    crm = LocalCRM(con, "Europe/Berlin")
    assert con.execute("SELECT current_setting('TimeZone')").fetchone()[0] == "Europe/Berlin"
    crm.aufgabe(1, "x", datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc))
    assert crm.aufgaben(1)[0]["faellig_am"] == datetime(2026, 7, 1, 12, 0)             # Sommerzeit: +2
    cid = crm.anruf_protokollieren(1, datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc), None, "x", 0, 0, [], [])
    assert con.execute("SELECT start FROM calls WHERE id = ?", [cid]).fetchone()[0] == datetime(2026, 1, 1, 11, 0)
    slot = crm.freie_slots(1)[0]
    assert crm.slot_per_start(crm.start_utc(slot["start"]))["slot_id"] == slot["slot_id"]
    crm2 = LocalCRM(agent.init_db(reset=True, path=":memory:"), "UTC")
    assert crm2.start_utc(datetime(2026, 7, 1, 9, 0)) == datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)


def test_rebooking_keeps_past_appointments_as_history(crm):
    tools = crm.tools(1)
    tools.update_lead(status="qualifiziert", eigentuemer=True, gebaeudetyp="einfamilienhaus")
    slots = tools.check_slots()["freie_termine"]
    assert tools.book_appointment(slot_id=slots[0]["slot_id"])["ok"]
    crm.con.execute("UPDATE slots SET start = start - INTERVAL 30 DAY WHERE id = ?", [slots[0]["slot_id"]])  # Termin liegt zurück
    r = crm.tools(1).book_appointment(slot_id=slots[1]["slot_id"])
    assert r["ok"] and "umgebucht_von" not in r                                           # Folgetermin, keine Umbuchung
    assert crm.con.execute("SELECT COUNT(*) FROM appointments WHERE lead_id = 1").fetchone()[0] == 2
