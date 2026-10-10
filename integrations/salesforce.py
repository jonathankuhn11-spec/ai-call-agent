"""Salesforce-Adapter: Lead lesen (per ID oder Telefonnummer), Felder schreiben, Anruf protokollieren,
Rückruf-Aufgabe anlegen. Gleiche Schnittstelle wie LocalCRM und HubSpotCRM, gegen die REST API
(sobjects, SOQL-Query) in der Form, wie Salesforce sie dokumentiert.

Auth: OAuth 2.0 Client-Credentials-Flow (Connected App mit "Client Credentials" aktiviert):
POST {login_url}/services/oauth2/token mit grant_type=client_credentials liefert access_token und
instance_url. Das Token wird gehalten und bei 401 einmal erneuert. Alternativ kann ein fertiges
Token mitgegeben werden (Tests, Sandbox-Sessions).

Objektmodell: Der Agent arbeitet auf dem Lead-Objekt (vor der Konvertierung). Notizen werden als
abgeschlossene Aufgaben vom Typ Anruf protokolliert ("Log a Call": Task mit Status Completed,
TaskSubtype Call), Rückrufe als offene Aufgaben mit Fälligkeitsdatum; beides mit WhoId auf den
Lead. Opt-outs setzen das Standardfeld DoNotCall. Eigene Felder (PV_*__c) müssen im Org
existieren; das Mapping ist Daten, nicht Code.

Getestet gegen die API-Form mit httpx.MockTransport, nicht gegen eine Live-Org. telli hat eine
native Salesforce-Synchronisation für Kontakte und Leads; dieser Adapter übernimmt, was sie
nicht tut: das Gesprächsergebnis nach den Regeln des Kunden in Status, Felder und Aufgaben
übersetzen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

API_VERSION = "v60.0"


class SalesforceFehler(Exception):
    pass


@dataclass
class SalesforceMapping:
    """Agentenfeld -> Lead-Feld. Werte gehen typgerecht (bool bleibt bool, Zahlen bleiben Zahlen)."""
    felder: dict[str, str] = field(default_factory=lambda: {
        "eigentuemer": "PV_Eigentuemer__c",
        "gebaeudetyp": "PV_Gebaeudetyp__c",
        "dachflaeche_m2": "PV_Dachflaeche_m2__c",
        "jahresverbrauch_kwh": "PV_Jahresverbrauch_kWh__c",
        "status": "Status",
        "ergebnis": "PV_Letztes_Ergebnis__c",
    })
    # Status des Agenten -> Lead-Status (Standard-Picklist eines Orgs; im Workshop festlegen)
    statuswerte: dict[str, str] = field(default_factory=lambda: {
        "offen": "Open - Not Contacted",
        "in_qualifizierung": "Working - Contacted",
        "qualifiziert": "Working - Contacted",
        "disqualifiziert": "Closed - Not Converted",
        "rueckruf": "Working - Contacted",
        "kein_interesse": "Closed - Not Converted",
        "opt_out": "Closed - Not Converted",
        "nicht_erreicht": "Open - Not Contacted",
    })
    lesen: dict[str, str] = field(default_factory=lambda: {   # Lead-Feld -> Lead-Dict des Agenten
        "FirstName": "vorname", "LastName": "nachname", "Phone": "telefon", "PostalCode": "plz",
        "LeadSource": "quelle", "Status": "sf_status", "PV_Eigentuemer__c": "eigentuemer",
        "PV_Gebaeudetyp__c": "gebaeudetyp", "PV_Dachflaeche_m2__c": "dachflaeche_m2",
        "PV_Jahresverbrauch_kWh__c": "jahresverbrauch_kwh", "DoNotCall": "do_not_call",
    })

    def nach_salesforce(self, felder: dict[str, Any]) -> dict[str, Any]:
        aus: dict[str, Any] = {}
        for k, v in felder.items():
            if v is None or k not in self.felder:
                continue
            if k == "status":
                aus[self.felder[k]] = self.statuswerte.get(v, "Working - Contacted")
                if v == "opt_out":
                    aus["DoNotCall"] = True
            else:
                aus[self.felder[k]] = v
        return aus

    def aus_salesforce(self, datensatz: dict[str, Any]) -> dict[str, Any]:
        lead: dict[str, Any] = {"id": datensatz.get("Id")}
        for sf, intern in self.lesen.items():
            if datensatz.get(sf) not in (None, ""):
                lead[intern] = datensatz[sf]
        lead["name"] = " ".join(x for x in (lead.pop("vorname", None), lead.pop("nachname", None)) if x) or None
        if "eigentuemer" in lead:
            lead["eigentuemer"] = bool(lead["eigentuemer"])
        for zahl in ("dachflaeche_m2", "jahresverbrauch_kwh"):
            if zahl in lead:
                try:
                    lead[zahl] = float(lead[zahl]) if zahl == "dachflaeche_m2" else int(float(lead[zahl]))
                except (TypeError, ValueError):
                    lead.pop(zahl)
        sf_status = lead.pop("sf_status", None)
        lead["status"] = next((k for k, v in self.statuswerte.items() if v == sf_status), "offen")
        if lead.pop("do_not_call", False):
            lead["status"] = "opt_out"
        return lead


class SalesforceCRM:
    def __init__(self, instance_url: str = "", token: str = "", login_url: str = "https://login.salesforce.com",
                 client_id: str = "", client_secret: str = "", mapping: Optional[SalesforceMapping] = None,
                 client: Optional[httpx.Client] = None, timeout: float = 8.0):
        self.mapping = mapping or SalesforceMapping()
        self.login_url, self.client_id, self.client_secret = login_url.rstrip("/"), client_id, client_secret
        self.instance_url, self.token = instance_url.rstrip("/"), token
        self.client = client or httpx.Client(timeout=timeout)

    # ------------------------------------------------------------ Auth
    def _anmelden(self):
        if not (self.client_id and self.client_secret):
            raise SalesforceFehler("Kein Token und keine Client-Credentials konfiguriert")
        try:
            r = self.client.post(f"{self.login_url}/services/oauth2/token",
                                 data={"grant_type": "client_credentials", "client_id": self.client_id,
                                       "client_secret": self.client_secret})
        except httpx.HTTPError as e:
            raise SalesforceFehler(f"Anmeldung: {e}") from e
        if r.status_code >= 400:
            raise SalesforceFehler(f"Anmeldung: HTTP {r.status_code} {r.text[:200]}")
        daten = r.json()
        self.token, self.instance_url = daten["access_token"], daten["instance_url"].rstrip("/")

    def _anfrage(self, methode: str, pfad: str, _erneut: bool = True, **kw) -> Any:
        if not self.token:
            self._anmelden()
        try:
            r = self.client.request(methode, f"{self.instance_url}{pfad}",
                                    headers={"Authorization": f"Bearer {self.token}"}, **kw)
        except httpx.HTTPError as e:
            raise SalesforceFehler(f"{methode} {pfad}: {e}") from e
        if r.status_code == 401 and _erneut and self.client_id:     # Token abgelaufen: einmal erneuern
            self.token = ""
            return self._anfrage(methode, pfad, _erneut=False, **kw)
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise SalesforceFehler(f"{methode} {pfad}: HTTP {r.status_code} {r.text[:200]}")
        return r.json() if r.content else {}

    # ------------------------------------------------------------ CRM-Schnittstelle
    def _felder(self) -> list[str]:
        return ["Id", *self.mapping.lesen]

    def lead(self, lead_id: Any) -> Optional[dict]:
        daten = self._anfrage("GET", f"/services/data/{API_VERSION}/sobjects/Lead/{lead_id}",
                              params={"fields": ",".join(self._felder())})
        return self.mapping.aus_salesforce(daten) if daten else None

    def lead_per_telefon(self, telefon: str) -> Optional[dict]:
        sicher = telefon.replace("\\", "").replace("'", "")
        soql = f"SELECT {', '.join(self._felder())} FROM Lead WHERE Phone = '{sicher}' AND IsConverted = false LIMIT 1"
        daten = self._anfrage("GET", f"/services/data/{API_VERSION}/query", params={"q": soql})
        records = (daten or {}).get("records") or []
        return self.mapping.aus_salesforce(records[0]) if records else None

    def lead_aktualisieren(self, lead_id: Any, **felder) -> dict:
        daten = self.mapping.nach_salesforce(felder)
        if not daten:
            return {"ok": True, "gespeichert": {}}
        r = self._anfrage("PATCH", f"/services/data/{API_VERSION}/sobjects/Lead/{lead_id}", json=daten)   # 204 No Content
        if r is None:                                                      # 404: Lead gelöscht oder konvertiert
            raise SalesforceFehler(f"Lead {lead_id} nicht gefunden (gelöscht oder konvertiert)")
        return {"ok": True, "gespeichert": daten}

    def notiz(self, lead_id: Any, text: str) -> Any:
        """"Log a Call": abgeschlossene Aufgabe vom Typ Anruf am Lead."""
        body = {"WhoId": str(lead_id), "Subject": "KI-Anruf", "Description": text[:32000], "Status": "Completed",
                "TaskSubtype": "Call", "ActivityDate": _datum(datetime.now(timezone.utc))}
        return (self._anfrage("POST", f"/services/data/{API_VERSION}/sobjects/Task", json=body) or {}).get("id")

    def aufgabe(self, lead_id: Any, titel: str, faellig: datetime, text: str = "") -> Any:
        body = {"WhoId": str(lead_id), "Subject": titel[:255], "Description": text[:32000], "Status": "Not Started",
                "Priority": "High", "TaskSubtype": "Call", "ActivityDate": _datum(faellig)}
        return (self._anfrage("POST", f"/services/data/{API_VERSION}/sobjects/Task", json=body) or {}).get("id")


def _datum(dt: datetime, zeitzone: str = "Europe/Berlin") -> str:
    """ActivityDate ist ein Kalendertag: der Tag in der Zeitzone des Kunden, nicht der UTC-Tag."""
    from zoneinfo import ZoneInfo
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(zeitzone)).strftime("%Y-%m-%d")
