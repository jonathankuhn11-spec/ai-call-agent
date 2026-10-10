"""Richtung CRM -> Plattform: Leads als Kontakte anlegen und Anrufe planen.

Speed-to-Lead ist die erste KPI im Scoping (unter 5 Minuten). Sie entscheidet sich nicht im
Gespräch, sondern hier: Ein neuer Lead im CRM muss innerhalb von Minuten als Kontakt bei
telli stehen und im Dialer liegen. `LeadPush.synchronisieren()` läuft dafür im Takt des
Servers (scheduler in server.py) oder als Batch über die Kommandozeile.

Endpunkte (docs.telli.com):
  POST /v2/contacts        Create Contact, camelCase, properties als [{key, value}]; 409 bei doppelter externalId
  POST /v1/schedule-call   Schedule Call im Dialer-Fenster; Antwort mit loop_id
  GET  /v1/get-call/{id}   Get Call: Metadaten, Transkript, Outcomes, Termine (für den Abgleich nach Webhook-Lücken)
Auth: Authorization: Bearer <API-Key>.
"""
from __future__ import annotations

from contextlib import nullcontext
from typing import Any, Optional

import httpx

from integrations.modelle import TelliAnrufPlanen, TelliKontaktAnlegen, telefon_normalisieren

LOOP_UNBEKANNT = "unbekannt"     # Anruf geplant, Antwort ohne loop_id: trotzdem nicht erneut planen


class TelliFehler(Exception):
    def __init__(self, nachricht: str, status: int = 0, code: str = ""):
        super().__init__(nachricht)
        self.status, self.code = status, code


def _json(r: httpx.Response) -> dict:
    try:
        daten = r.json()
    except ValueError:
        return {}
    return daten if isinstance(daten, dict) else {}


class TelliClient:
    def __init__(self, api_key: str, basis_url: str = "https://api.telli.com",
                 client: Optional[httpx.Client] = None, timeout: float = 10.0):
        self.client = client or httpx.Client(base_url=basis_url, timeout=timeout)
        self.client.headers["Authorization"] = f"Bearer {api_key}"

    def _post(self, pfad: str, body: dict) -> dict:
        try:
            r = self.client.post(pfad, json=body)
        except httpx.HTTPError as e:
            raise TelliFehler(f"POST {pfad}: {e}") from e
        daten = _json(r)
        if r.status_code >= 400:
            raise TelliFehler(daten.get("message") or f"HTTP {r.status_code}", r.status_code, daten.get("code", ""))
        return daten

    def kontakt_anlegen(self, kontakt: TelliKontaktAnlegen) -> dict:
        daten = self._post("/v2/contacts", kontakt.model_dump(exclude_none=True))
        if not daten.get("id"):
            raise TelliFehler("Create Contact ohne id in der Antwort", 200)
        return daten

    def anruf_planen(self, planung: TelliAnrufPlanen) -> dict:
        return self._post("/v1/schedule-call", planung.model_dump(exclude_none=True))

    def anruf(self, call_id: str) -> dict:
        try:
            r = self.client.get(f"/v1/get-call/{call_id}")
        except httpx.HTTPError as e:
            raise TelliFehler(f"GET get-call: {e}") from e
        if r.status_code >= 400:
            raise TelliFehler(_json(r).get("message") or f"HTTP {r.status_code}", r.status_code)
        return _json(r)


def kontakt_aus_lead(lead: dict, zeitzone: str = "Europe/Berlin") -> TelliKontaktAnlegen:
    """Lead-Dict des CRM -> Create-Contact-Body. Eigenschaften müssen bei telli unter
    Settings -> Contact properties angelegt sein (siehe docs/playbook/02-integrations-checkliste.md)."""
    name = (lead.get("name") or "").strip()
    vorname, _, nachname = name.partition(" ")
    eigenschaften = [{"key": k, "value": lead[k]} for k in ("quelle", "plz", "gebaeudetyp") if lead.get(k)]
    if lead.get("eigentuemer") is not None:
        eigenschaften.append({"key": "eigentuemer", "value": bool(lead["eigentuemer"])})
    return TelliKontaktAnlegen(
        firstName=(vorname or "Unbekannt")[:50], lastName=(nachname or "Unbekannt")[:50],
        phoneNumber=telefon_normalisieren(lead.get("telefon")), externalId=str(lead["id"]),
        timezoneIana=zeitzone, properties=eigenschaften)


class LeadPush:
    """Neue Leads anlegen und anrufen lassen. Idempotent: Was einen telli-Kontakt hat, wird übersprungen.

    `sperre` schützt die geteilte Datenbankverbindung; die HTTP-Aufrufe zu telli laufen ohne Sperre.
    """

    def __init__(self, crm, client: TelliClient, agent_id: str, max_retry_days: Optional[int] = None, sperre=None):
        self.crm, self.client, self.agent_id, self.max_retry_days = crm, client, agent_id, max_retry_days
        self.sperre = sperre or nullcontext()

    def synchronisieren(self) -> dict[str, Any]:
        bericht: dict[str, Any] = {"angelegt": [], "geplant": [], "uebersprungen": [], "fehler": []}
        bearbeitet: set[int] = set()
        with self.sperre:
            kandidaten = self.crm.leads_fuer_push()
        for lead in kandidaten:
            bearbeitet.add(lead["id"])
            try:
                kontakt = self.client.kontakt_anlegen(kontakt_aus_lead(lead))
            except TelliFehler as e:
                if e.status == 409:   # Kontakt existiert schon: einmal melden, nicht bei jedem Lauf erneut versuchen
                    with self.sperre:
                        self.crm.telli_kontakt_speichern(lead["id"], None)
                    bericht["uebersprungen"].append({"lead_id": lead["id"], "grund": "externalId existiert bei telli, "
                                                                                      "Kontakt-ID manuell zuordnen"})
                else:
                    bericht["fehler"].append({"lead_id": lead["id"], "fehler": str(e)})
                continue
            contact_id = str(kontakt["id"])
            with self.sperre:
                self.crm.telli_kontakt_speichern(lead["id"], contact_id)
            bericht["angelegt"].append({"lead_id": lead["id"], "contact_id": contact_id})
            self._planen(lead["id"], contact_id, bericht)
        with self.sperre:
            nachzuegler = self.crm.leads_ohne_anrufplanung()
        for lead in nachzuegler:                          # Kontakt aus früherem Lauf, Anruf fehlt: nachholen
            if lead["id"] not in bearbeitet:
                self._planen(lead["id"], lead["contact_id"], bericht)
        return bericht

    def _planen(self, lead_id: int, contact_id: str, bericht: dict[str, Any]) -> None:
        try:
            planung = self.client.anruf_planen(TelliAnrufPlanen(contact_id=contact_id, agent_id=self.agent_id,
                                                               max_retry_days=self.max_retry_days))
        except TelliFehler as e:
            bericht["fehler"].append({"lead_id": lead_id, "fehler": f"Anruf nicht geplant: {e}"})
            return
        loop_id = str(planung.get("loop_id") or LOOP_UNBEKANNT)
        with self.sperre:
            self.crm.telli_kontakt_speichern(lead_id, contact_id, loop_id)
        bericht["geplant"].append({"lead_id": lead_id, "loop_id": loop_id})
