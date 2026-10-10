"""Richtung CRM -> Plattform: Create Contact v2 und Schedule Call v1, Lead-Push idempotent und fehlertolerant."""
import json

import httpx
import pytest

from integrations.crm import LocalCRM
from integrations.modelle import TelliAnrufPlanen, TelliKontaktAnlegen
from integrations.telli import LeadPush, TelliClient, TelliFehler, kontakt_aus_lead


@pytest.fixture
def telli():
    requests = []
    zustand = {"doppelt": set(), "ausfall_schedule": False}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body, dict(request.headers)))
        if request.url.path == "/v2/contacts":
            if body["externalId"] in zustand["doppelt"]:
                return httpx.Response(409, json={"code": "DUPLICATE_EXTERNAL_ID", "message": "External ID already exists",
                                                 "data": {"externalId": body["externalId"]}})
            return httpx.Response(201, json={"id": f"ct_{body['externalId']}", "type": "Contact", "autoDialerStatus": "not_in_dialer"})
        if request.url.path == "/v1/schedule-call":
            if zustand["ausfall_schedule"]:
                return httpx.Response(400, json={"message": "Auto dialer is not enabled"})
            return httpx.Response(200, json={"status": "success", "contact_id": body["contact_id"], "loop_id": f"loop_{body['contact_id']}"})
        if request.url.path.startswith("/v1/get-call/"):
            return httpx.Response(200, json={"call": {"call_id": request.url.path.rsplit("/", 1)[1]}, "contact": {}})
        return httpx.Response(404, json={"message": "nicht da"})

    client = TelliClient("telli-key", client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telli.com"))
    client.requests, client.zustand = requests, zustand
    return client


def test_contact_body_is_camel_case_and_strict():
    lead = {"id": 7, "name": "Anna Schröder", "telefon": "0251 000004", "plz": "48143", "quelle": "Website-Formular",
            "eigentuemer": True, "gebaeudetyp": None}
    k = kontakt_aus_lead(lead)
    assert k.model_dump(exclude_none=True) == {
        "firstName": "Anna", "lastName": "Schröder", "phoneNumber": "+49251000004", "externalId": "7",
        "timezoneIana": "Europe/Berlin",
        "properties": [{"key": "quelle", "value": "Website-Formular"}, {"key": "plz", "value": "48143"},
                       {"key": "eigentuemer", "value": True}]}
    assert kontakt_aus_lead({"id": 8, "name": "Cher", "telefon": "+1"}).lastName == "Unbekannt"
    with pytest.raises(Exception):
        TelliKontaktAnlegen(firstName="A", lastName="B", phoneNumber="+1", first_name="falsch")     # snake_case abgelehnt


def test_client_sends_bearer_and_documented_paths(telli):
    k = telli.kontakt_anlegen(TelliKontaktAnlegen(firstName="Max", lastName="Muster", phoneNumber="+4915112345678", externalId="1"))
    methode, pfad, body, kopf = telli.requests[-1]
    assert (methode, pfad) == ("POST", "/v2/contacts") and kopf["authorization"] == "Bearer telli-key"
    assert body == {"firstName": "Max", "lastName": "Muster", "phoneNumber": "+4915112345678", "externalId": "1", "properties": []}
    assert k["id"] == "ct_1"
    p = telli.anruf_planen(TelliAnrufPlanen(contact_id="ct_1", agent_id="agent_x", max_retry_days=3,
                                            schedule={"at": "2026-10-12T08:00:00.000Z", "ignore_dialing_window": False}))
    methode, pfad, body, _ = telli.requests[-1]
    assert (methode, pfad) == ("POST", "/v1/schedule-call") and p["loop_id"] == "loop_ct_1"
    assert body == {"contact_id": "ct_1", "agent_id": "agent_x", "max_retry_days": 3,
                    "schedule": {"at": "2026-10-12T08:00:00.000Z", "ignore_dialing_window": False}}
    assert telli.anruf("abc")["call"]["call_id"] == "abc"
    telli.zustand["doppelt"].add("dup")
    with pytest.raises(TelliFehler) as e:
        telli.kontakt_anlegen(TelliKontaktAnlegen(firstName="M", lastName="M", phoneNumber="+1", externalId="dup"))
    assert e.value.status == 409 and e.value.code == "DUPLICATE_EXTERNAL_ID"


def test_lead_push_is_idempotent_and_skips_blocked_and_duplicate_leads(con, telli):
    crm = LocalCRM(con)
    crm.sperren("+49 251 000005", "Opt-out")
    telli.zustand["doppelt"].add("2")
    push = LeadPush(crm, telli, agent_id="agent_x", max_retry_days=5)
    b = push.synchronisieren()
    assert [a["lead_id"] for a in b["angelegt"]] == [1, 3, 4]
    assert [g["lead_id"] for g in b["geplant"]] == [1, 3, 4]
    assert b["uebersprungen"][0]["lead_id"] == 2 and "manuell" in b["uebersprungen"][0]["grund"] and b["fehler"] == []
    assert crm.telli_kontakt(1) == {"contact_id": "ct_1", "loop_id": "loop_ct_1"} and crm.telli_kontakt(5) is None
    assert crm.zuordnung_offen() == [2]
    geplant = [r for r in telli.requests if r[1] == "/v1/schedule-call"]
    assert geplant[0][2] == {"contact_id": "ct_1", "agent_id": "agent_x", "max_retry_days": 5}
    zweiter = push.synchronisieren()                                                         # zweiter Lauf: nichts doppelt
    assert zweiter["angelegt"] == [] and zweiter["geplant"] == [] and zweiter["uebersprungen"] == []
    assert len([r for r in telli.requests if r[1] == "/v2/contacts"]) == 4                  # 3 angelegt + 1 Duplikat, kein Retry


def test_lead_push_keeps_the_contact_when_scheduling_fails(con, telli):
    crm = LocalCRM(con)
    telli.zustand["ausfall_schedule"] = True
    b = LeadPush(crm, telli, agent_id="agent_x").synchronisieren()
    assert len(b["angelegt"]) == 5 and b["geplant"] == [] and len(b["fehler"]) == 5
    assert "Auto dialer" in b["fehler"][0]["fehler"]
    assert crm.telli_kontakt(1) == {"contact_id": "ct_1", "loop_id": None}
    telli.zustand["ausfall_schedule"] = False
    b = LeadPush(crm, telli, agent_id="agent_x").synchronisieren()
    assert b["angelegt"] == [] and len(b["geplant"]) == 5                                   # Kontakte da, Anrufe nachgeholt
    assert crm.telli_kontakt(1)["loop_id"] == "loop_ct_1"
    assert len([r for r in telli.requests if r[1] == "/v2/contacts"]) == 5


def test_client_is_robust_against_unexpected_api_shapes(con):
    antworten = {"contacts": httpx.Response(201, json={"type": "Contact"}),                 # ohne id
                 "schedule": httpx.Response(200, json={"status": "success"}),               # ohne loop_id
                 "text": httpx.Response(502, text="<html>Bad Gateway</html>")}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/contacts":
            return antworten["contacts"]
        if request.url.path == "/v1/schedule-call":
            return antworten["schedule"]
        return antworten["text"]

    client = TelliClient("k", client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telli.com"))
    crm = LocalCRM(con)
    b = LeadPush(crm, client, agent_id="a").synchronisieren()
    assert len(b["fehler"]) == 5 and "ohne id" in b["fehler"][0]["fehler"] and crm.telli_kontakt(1) is None   # nichts halb gespeichert
    antworten["contacts"] = httpx.Response(201, json={"id": "ct_x"})
    b = LeadPush(crm, client, agent_id="a").synchronisieren()
    assert len(b["geplant"]) == 5 and crm.telli_kontakt(1) == {"contact_id": "ct_x", "loop_id": "unbekannt"}
    assert LeadPush(crm, client, agent_id="a").synchronisieren()["geplant"] == []        # kein erneutes Planen ohne loop_id
    with pytest.raises(TelliFehler) as e:
        client.anruf("abc")
    assert e.value.status == 502 and "HTTP 502" in str(e.value)
