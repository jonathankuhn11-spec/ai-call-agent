"""HubSpot-Adapter: Kontakt lesen (per ID oder Telefonnummer), Eigenschaften schreiben, Notiz und
Aufgabe anlegen. Gleiche Schnittstelle wie LocalCRM, gegen die dokumentierte CRM-API v3.

Feldmapping ist Daten, kein Code: `Feldmapping` übersetzt die Felder des Agenten in
HubSpot-Eigenschaften und zurück. Was hier als Standard steht, wird im Scoping-Workshop mit dem
Kunden festgelegt (eigene Eigenschaften müssen im Portal existieren, Lead-Status-Werte müssen
zu seinem Vertriebsprozess passen).

Getestet gegen die API-Form mit httpx.MockTransport (Pfad, Methode, Body), nicht gegen einen
Live-Account. telli hat eine native HubSpot-Synchronisation für Kontakte; dieser Adapter
übernimmt, was die native Synchronisation nicht tut: Gesprächsergebnis nach den Regeln des
Kunden in Lead-Status, Notiz und Rückruf-Aufgabe übersetzen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

NOTE_TO_CONTACT = 202   # HubSpot-definierte Assoziationstypen (Standardwerte des Portals)
TASK_TO_CONTACT = 204


class HubSpotFehler(Exception):
    pass


@dataclass
class Feldmapping:
    """Agentenfeld -> HubSpot-Eigenschaft. Werte gehen als Strings, so will es die API."""
    eigenschaften: dict[str, str] = field(default_factory=lambda: {
        "eigentuemer": "pv_eigentuemer",
        "gebaeudetyp": "pv_gebaeudetyp",
        "dachflaeche_m2": "pv_dachflaeche_m2",
        "jahresverbrauch_kwh": "pv_jahresverbrauch_kwh",
        "status": "hs_lead_status",
        "ergebnis": "pv_letztes_ergebnis",
        "notiz": "pv_agent_notiz",
    })
    # Status des Agenten -> Lead-Status in HubSpot (Standardoptionen des Portals)
    statuswerte: dict[str, str] = field(default_factory=lambda: {
        "offen": "NEW",
        "in_qualifizierung": "IN_PROGRESS",
        "qualifiziert": "CONNECTED",
        "disqualifiziert": "UNQUALIFIED",
        "rueckruf": "BAD_TIMING",
        "kein_interesse": "UNQUALIFIED",
        "opt_out": "UNQUALIFIED",
        "nicht_erreicht": "ATTEMPTED_TO_CONTACT",
    })
    opt_out_eigenschaft: str = "pv_opt_out"
    lesen: dict[str, str] = field(default_factory=lambda: {   # HubSpot -> Lead-Dict des Agenten
        "firstname": "vorname", "lastname": "nachname", "phone": "telefon", "zip": "plz",
        "hs_lead_status": "hubspot_status", "pv_eigentuemer": "eigentuemer", "pv_gebaeudetyp": "gebaeudetyp",
        "pv_dachflaeche_m2": "dachflaeche_m2", "pv_jahresverbrauch_kwh": "jahresverbrauch_kwh",
        "hs_analytics_source": "quelle",
    })

    def nach_hubspot(self, felder: dict[str, Any]) -> dict[str, str]:
        aus: dict[str, str] = {}
        for k, v in felder.items():
            if v is None or k not in self.eigenschaften:
                continue
            if k == "status":
                aus[self.eigenschaften[k]] = self.statuswerte.get(v, "IN_PROGRESS")
                if v == "opt_out":
                    aus[self.opt_out_eigenschaft] = "true"
            elif isinstance(v, bool):
                aus[self.eigenschaften[k]] = "true" if v else "false"
            else:
                aus[self.eigenschaften[k]] = str(v)
        return aus

    def aus_hubspot(self, kontakt: dict[str, Any]) -> dict[str, Any]:
        props = kontakt.get("properties", {}) or {}
        lead: dict[str, Any] = {"id": kontakt.get("id")}
        for hs, intern in self.lesen.items():
            if props.get(hs) not in (None, ""):
                lead[intern] = props[hs]
        lead["name"] = " ".join(x for x in (lead.pop("vorname", None), lead.pop("nachname", None)) if x) or None
        if "eigentuemer" in lead:
            lead["eigentuemer"] = str(lead["eigentuemer"]).lower() == "true"
        for zahl in ("dachflaeche_m2", "jahresverbrauch_kwh"):
            if zahl in lead:
                try:
                    lead[zahl] = float(lead[zahl]) if zahl == "dachflaeche_m2" else int(float(lead[zahl]))
                except ValueError:
                    lead.pop(zahl)
        lead["status"] = _rueckwaerts(self.statuswerte, lead.pop("hubspot_status", None))
        return lead


def _rueckwaerts(statuswerte: dict[str, str], hs_status: Optional[str]) -> str:
    if not hs_status:
        return "offen"
    for intern, hs in statuswerte.items():
        if hs == hs_status:
            return intern
    return "offen"


class HubSpotCRM:
    def __init__(self, token: str, basis_url: str = "https://api.hubapi.com", mapping: Optional[Feldmapping] = None,
                 client: Optional[httpx.Client] = None, timeout: float = 5.0):
        self.mapping = mapping or Feldmapping()
        self.client = client or httpx.Client(base_url=basis_url, timeout=timeout,
                                             headers={"Authorization": f"Bearer {token}"})
        if client is not None and "Authorization" not in client.headers:
            client.headers["Authorization"] = f"Bearer {token}"

    def _anfrage(self, methode: str, pfad: str, **kw) -> dict:
        try:
            r = self.client.request(methode, pfad, **kw)
        except httpx.HTTPError as e:
            raise HubSpotFehler(f"{methode} {pfad}: {e}") from e
        if r.status_code == 404:
            return {}
        if r.status_code >= 400:
            raise HubSpotFehler(f"{methode} {pfad}: HTTP {r.status_code} {r.text[:200]}")
        return r.json() if r.content else {}

    def _eigenschaften(self) -> list[str]:
        return list(self.mapping.lesen)

    def lead(self, lead_id: Any) -> Optional[dict]:
        daten = self._anfrage("GET", f"/crm/v3/objects/contacts/{lead_id}",
                              params={"properties": ",".join(self._eigenschaften())})
        return self.mapping.aus_hubspot(daten) if daten else None

    def lead_per_telefon(self, telefon: str) -> Optional[dict]:
        body = {"filterGroups": [{"filters": [{"propertyName": "phone", "operator": "EQ", "value": telefon}]}],
                "properties": self._eigenschaften(), "limit": 1}
        daten = self._anfrage("POST", "/crm/v3/objects/contacts/search", json=body)
        ergebnisse = daten.get("results") or []
        return self.mapping.aus_hubspot(ergebnisse[0]) if ergebnisse else None

    def lead_aktualisieren(self, lead_id: Any, **felder) -> dict:
        props = self.mapping.nach_hubspot(felder)
        if not props:
            return {"ok": True, "gespeichert": {}}
        self._anfrage("PATCH", f"/crm/v3/objects/contacts/{lead_id}", json={"properties": props})
        return {"ok": True, "gespeichert": props}

    def notiz(self, lead_id: Any, text: str) -> Any:
        body = {"properties": {"hs_timestamp": _jetzt_iso(), "hs_note_body": text[:65000]},
                "associations": [{"to": {"id": str(lead_id)},
                                  "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": NOTE_TO_CONTACT}]}]}
        return self._anfrage("POST", "/crm/v3/objects/notes", json=body).get("id")

    def aufgabe(self, lead_id: Any, titel: str, faellig: datetime, text: str = "") -> Any:
        body = {"properties": {"hs_timestamp": _iso(faellig), "hs_task_subject": titel[:200], "hs_task_body": text[:2000],
                               "hs_task_status": "NOT_STARTED", "hs_task_priority": "HIGH", "hs_task_type": "CALL"},
                "associations": [{"to": {"id": str(lead_id)},
                                  "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": TASK_TO_CONTACT}]}]}
        return self._anfrage("POST", "/crm/v3/objects/tasks", json=body).get("id")


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jetzt_iso() -> str:
    return _iso(datetime.now(timezone.utc))
