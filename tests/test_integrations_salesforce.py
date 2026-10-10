"""Salesforce-Adapter: OAuth-Client-Credentials, SOQL-Suche, Lead-PATCH, Aufgaben; gegen die API-Form."""
import json
from datetime import datetime, timezone

import httpx
import pytest

from integrations.salesforce import API_VERSION, SalesforceCRM, SalesforceFehler, SalesforceMapping


@pytest.fixture
def salesforce():
    zustand = {"requests": [], "token_ablaeufe": 0, "tokens": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content and request.headers.get("content-type", "").startswith("application/json") else None
        zustand["requests"].append((request.method, str(request.url), body, dict(request.headers)))
        if request.url.path == "/services/oauth2/token":
            zustand["tokens"] += 1
            assert b"grant_type=client_credentials" in request.content
            return httpx.Response(200, json={"access_token": f"tok_{zustand['tokens']}", "instance_url": "https://kunde.my.salesforce.com",
                                             "token_type": "Bearer"})
        if zustand["token_ablaeufe"] > 0:
            zustand["token_ablaeufe"] -= 1
            return httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID"}])
        if request.url.path.endswith("/query"):
            return httpx.Response(200, json={"totalSize": 1, "done": True, "records": [{
                "attributes": {"type": "Lead"}, "Id": "00Q1", "FirstName": "Thomas", "LastName": "Becker",
                "Phone": "+49251000001", "PostalCode": "48149", "LeadSource": "Web", "Status": "Working - Contacted",
                "PV_Eigentuemer__c": True, "PV_Jahresverbrauch_kWh__c": 4200.0, "DoNotCall": False}]})
        if request.url.path.endswith("/sobjects/Lead/404"):
            return httpx.Response(404, json=[{"errorCode": "NOT_FOUND"}])
        if request.method == "PATCH":
            return httpx.Response(204)
        if request.url.path.endswith("/sobjects/Task"):
            return httpx.Response(201, json={"id": "00T9", "success": True, "errors": []})
        if request.method == "GET":
            return httpx.Response(200, json={"Id": "00Q1", "FirstName": "Thomas", "LastName": "Becker", "DoNotCall": True})
        return httpx.Response(500, text="kaputt")

    crm = SalesforceCRM(login_url="https://login.salesforce.com", client_id="cid", client_secret="geheim",
                        client=httpx.Client(transport=httpx.MockTransport(handler)))
    crm.zustand = zustand
    return crm


def test_client_credentials_login_and_token_refresh_on_401(salesforce):
    lead = salesforce.lead_per_telefon("+49251000001")
    pfade = [r[1] for r in salesforce.zustand["requests"]]
    assert pfade[0] == "https://login.salesforce.com/services/oauth2/token"
    assert pfade[1].startswith(f"https://kunde.my.salesforce.com/services/data/{API_VERSION}/query?q=SELECT")
    assert "WHERE+Phone+%3D+%27%2B49251000001%27" in pfade[1] and "IsConverted" in pfade[1]
    assert salesforce.zustand["requests"][1][3]["authorization"] == "Bearer tok_1"
    assert lead == {"id": "00Q1", "telefon": "+49251000001", "plz": "48149", "quelle": "Web", "eigentuemer": True,
                    "jahresverbrauch_kwh": 4200, "name": "Thomas Becker", "status": "in_qualifizierung"}
    salesforce.zustand["token_ablaeufe"] = 1
    assert salesforce.lead("00Q1")["status"] == "opt_out"                     # DoNotCall -> opt_out
    assert salesforce.zustand["tokens"] == 2                                   # einmal erneuert, dann Erfolg
    assert salesforce.zustand["requests"][-1][3]["authorization"] == "Bearer tok_2"


def test_update_note_and_task_follow_the_rest_api_shape(salesforce):
    r = salesforce.lead_aktualisieren("00Q1", status="opt_out", eigentuemer=False, dachflaeche_m2=70, unbekannt=1)
    methode, url, body, _ = salesforce.zustand["requests"][-1]
    assert (methode, url) == ("PATCH", f"https://kunde.my.salesforce.com/services/data/{API_VERSION}/sobjects/Lead/00Q1")
    assert body == {"Status": "Closed - Not Converted", "DoNotCall": True, "PV_Eigentuemer__c": False, "PV_Dachflaeche_m2__c": 70}
    assert r["ok"] and r["gespeichert"]["DoNotCall"] is True
    assert salesforce.notiz("00Q1", "KI-Anruf: opt_out") == "00T9"
    methode, url, body, _ = salesforce.zustand["requests"][-1]
    assert url.endswith("/sobjects/Task") and body["Status"] == "Completed" and body["TaskSubtype"] == "Call"
    assert body["WhoId"] == "00Q1" and body["Description"] == "KI-Anruf: opt_out"
    salesforce.aufgabe("00Q1", "Rückruf", datetime(2030, 1, 2, 9, tzinfo=timezone.utc), "morgen")
    body = salesforce.zustand["requests"][-1][2]
    assert body["Status"] == "Not Started" and body["ActivityDate"] == "2030-01-02" and body["Priority"] == "High"
    assert salesforce.lead("404") is None
    assert salesforce.lead_aktualisieren("00Q1", unbekannt=1) == {"ok": True, "gespeichert": {}}


def test_errors_and_missing_credentials():
    def kaputt(request):
        return httpx.Response(500, text="Server Error")

    crm = SalesforceCRM(instance_url="https://x.my.salesforce.com", token="t", client=httpx.Client(transport=httpx.MockTransport(kaputt)))
    with pytest.raises(SalesforceFehler, match="HTTP 500"):
        crm.lead("00Q1")
    with pytest.raises(SalesforceFehler, match="Client-Credentials"):
        SalesforceCRM(client=httpx.Client(transport=httpx.MockTransport(kaputt))).lead("00Q1")
    mapping = SalesforceMapping(statuswerte={"qualifiziert": "Qualified"})
    assert mapping.nach_salesforce({"status": "qualifiziert"}) == {"Status": "Qualified"}
    assert mapping.aus_salesforce({"Id": "1", "Status": "Qualified"})["status"] == "qualifiziert"


def test_soql_quotes_are_stripped_from_phone_lookups(salesforce):
    salesforce.lead_per_telefon("+49' OR Id != '")
    url = salesforce.zustand["requests"][-1][1]
    assert "%27%2B49+OR+Id+%21%3D+%27" in url and "%27%27" not in url     # keine Injektion über die Nummer
