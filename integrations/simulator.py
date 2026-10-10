"""Plattform-Simulator: spielt telli gegen die Integrationsschicht, ohne Account und ohne Netz.

Er schickt genau die Requests, die die Plattform schickt (Custom Tool mit Bearer-Token,
Custom Calendar und Contact Lookup mit x-telli-signature, call_ended mit Svix-Signatur), und
fährt damit komplette Pilotgespräche: Lead nachschlagen, Fakten speichern, Termine holen,
buchen, Gesprächsende melden. Der UAT in simulate.py prüft den Agenten; dieser Simulator
prüft die Integration drumherum. In den Tests läuft er gegen die App im Prozess
(fastapi.testclient), in der Demo ebenso.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from integrations.modelle import iso_utc
from integrations.signatur import svix_kopfzeilen, telli_signatur


class PlattformSimulator:
    def __init__(self, client: httpx.Client, api_key: str = "", webhook_secret: str = "", tool_secret: str = "",
                 agent_id: str = "agent_demo"):
        self.client, self.api_key, self.webhook_secret, self.tool_secret = client, api_key, webhook_secret, tool_secret
        self.agent_id = agent_id
        self.protokoll: list[dict[str, Any]] = []

    # ------------------------------------------------------------ Requests wie telli
    def _post(self, pfad: str, body: dict, signieren: bool = False, headers: Optional[dict[str, str]] = None) -> httpx.Response:
        """Signiert wird über exakt die Bytes, die gesendet werden; das ist der einzige korrekte Weg."""
        roh = json.dumps(body, ensure_ascii=False).encode()
        kopf = {"Content-Type": "application/json", **(headers or {})}
        if signieren and self.api_key:
            kopf["x-telli-signature"] = telli_signatur(self.api_key, roh)
        t0 = time.perf_counter()
        r = self.client.post(pfad, content=roh, headers=kopf)
        self.protokoll.append({"pfad": pfad, "status": r.status_code, "ms": round((time.perf_counter() - t0) * 1000, 1),
                               "antwort": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text})
        return r

    def kontakt(self, external_id: str, telefon: str, vorname: str = "Max", nachname: str = "Muster") -> dict:
        """Kontaktobjekt in der V2-Form, wie telli es Kalender-Endpunkten mitschickt."""
        return {"id": f"ct_{external_id}", "type": "Contact", "externalId": external_id, "externalUrl": None,
                "salutation": None, "firstName": vorname, "lastName": nachname, "email": None, "phoneNumber": telefon,
                "timezoneIana": "Europe/Berlin", "autoDialerStatus": "in_dialer",
                "createdAt": iso_utc(datetime.now(timezone.utc)), "updatedAt": iso_utc(datetime.now(timezone.utc)),
                "properties": []}

    def contact_lookup(self, telefon: str, to_number: str = "+4925100000") -> dict:
        body = {"event": "contact_lookup", "phone_number": telefon, "to_number": to_number}
        return self._post("/webhooks/contact-lookup", body, signieren=True).json()

    def tool(self, name: str, call_id: str, external_id: Optional[str], telefon: Optional[str] = None, **args) -> dict:
        body = {"call_id": call_id, "external_id": external_id, "phone_number": telefon, **args}
        kopf = {"Authorization": f"Bearer {self.tool_secret}"} if self.tool_secret else {}
        return self._post(f"/tools/{name}", body, headers=kopf).json()

    def available(self, external_id: str, telefon: str) -> dict:
        body = {"contact": self.kontakt(external_id, telefon), "contact_id": f"ct_{external_id}",
                "external_contact_id": external_id, "contact_details": {}}
        return self._post("/calendar/available", body, signieren=True).json()

    def book(self, external_id: str, telefon: str, start_iso: str) -> dict:
        body = {"contact": self.kontakt(external_id, telefon), "contact_id": f"ct_{external_id}",
                "external_contact_id": external_id, "contact_details": {}, "start_iso": start_iso}
        return self._post("/calendar/book", body, signieren=True).json()

    def call_ended(self, call_id: str, external_id: Optional[str], telefon: str, outcomes: dict[str, Any],
                   transkript: Optional[list[dict]] = None, status: str = "connected", attempt: int = 1,
                   appointments: Optional[list[dict]] = None, message_id: Optional[str] = None,
                   ended_reason: str = "agent-ended-call", zeitstempel: Optional[int] = None) -> httpx.Response:
        jetzt = datetime.now(timezone.utc)
        transkript = transkript if transkript is not None else [
            {"role": "agent", "content": "Guten Tag, hier ist Lena, die digitale KI-Assistentin von SonnenWerk Energie."},
            {"role": "user", "content": "Ja, hallo?"},
            {"role": "agent", "toolActivity": "lookup_lead", "toolParameters": {}},
            {"role": "user", "content": "Ja, das Haus gehört mir."},
        ]
        typen = {bool: "boolean", int: "number", float: "number"}
        body = {"event": "call_ended",
                "call": {"call_id": call_id, "attempt": attempt, "loop_id": f"loop_{call_id}", "from_number": "+4925100000",
                         "to_number": telefon, "direction": "outbound", "external_contact_id": external_id,
                         "contact_id": f"ct_{external_id}", "contact_details": {}, "agent_id": self.agent_id,
                         "triggered_at_iso": iso_utc(jetzt - timedelta(minutes=4)),
                         "started_at_iso": iso_utc(jetzt - timedelta(minutes=3)), "ended_at_iso": iso_utc(jetzt),
                         "call_length_min": 3, "state": "ended", "status": status, "ended_reason": ended_reason,
                         "follow_up": None, "transcript": "\n".join(f"{e['role']}: {e.get('content', '')}" for e in transkript),
                         "transcriptObject": transkript,
                         "outcomes": [{"key": k, "dataType": typen.get(type(v), "string"), "value": v} for k, v in outcomes.items()],
                         "collected_data": {}, "appointments": appointments or [], "recording_url": None},
                "contact": {"contact_id": f"ct_{external_id}", "external_contact_id": external_id, "first_name": "Max",
                            "last_name": "Muster", "phone_number": telefon, "status": "reached"}}
        roh = json.dumps(body, ensure_ascii=False).encode()
        kopf = {"Content-Type": "application/json"}
        msg_id = message_id or f"msg_{uuid.uuid4().hex[:12]}"
        if self.webhook_secret:
            kopf.update(svix_kopfzeilen(self.webhook_secret, msg_id, roh, zeitstempel))
        else:
            kopf["svix-id"] = msg_id
        t0 = time.perf_counter()
        r = self.client.post("/webhooks/telli", content=roh, headers=kopf)
        self.protokoll.append({"pfad": "/webhooks/telli", "status": r.status_code,
                               "ms": round((time.perf_counter() - t0) * 1000, 1), "antwort": r.json()})
        return r

    # ------------------------------------------------------------ Komplette Pilotgespräche
    def gespraech(self, lead: dict, persona: str = "happy_path") -> dict[str, Any]:
        """Ein Gespräch, wie die Plattform es gegen das Backend fährt. Rückgabe: Was passiert ist."""
        call_id, ext, tel = f"call_{uuid.uuid4().hex[:10]}", str(lead["id"]), lead["telefon"]
        bericht: dict[str, Any] = {"call_id": call_id, "persona": persona, "lead_id": lead["id"]}
        bericht["lookup"] = self.tool("lookup_lead", call_id, ext)
        if persona == "happy_path":
            self.tool("update_lead", call_id, ext, status="in_qualifizierung", eigentuemer=True, gebaeudetyp="einfamilienhaus")
            self.tool("update_lead", call_id, ext, status="in_qualifizierung", dachflaeche_m2=70, jahresverbrauch_kwh=4200)
            bericht["qualifiziert"] = self.tool("update_lead", call_id, ext, status="qualifiziert")
            bericht["available"] = self.available(ext, tel)
            erster = bericht["available"]["available"][0]["start_iso"]
            bericht["book"] = self.book(ext, tel, erster)
            outcomes = {"ergebnis": "termin_gebucht", "eigentuemer": True, "gebaeudetyp": "einfamilienhaus"}
        elif persona == "mieter":
            self.tool("update_lead", call_id, ext, status="in_qualifizierung", eigentuemer=False)
            bericht["disqualifiziert"] = self.tool("update_lead", call_id, ext, status="disqualifiziert")
            outcomes = {"ergebnis": "disqualifiziert", "eigentuemer": False}
        elif persona == "keine_zeit":
            morgen = iso_utc(datetime.now(timezone.utc) + timedelta(days=1))
            outcomes = {"ergebnis": "rueckruf_vereinbart", "rueckruf_zeitpunkt": morgen}
        elif persona == "opt_out":
            outcomes = {"ergebnis": "opt_out"}
        elif persona == "behauptet_termin":          # Agent behauptet eine Buchung, die es nicht gab
            outcomes = {"ergebnis": "termin_gebucht"}
        elif persona == "nicht_erreicht":
            r = self.call_ended(call_id, ext, tel, {}, transkript=[], status="voicemail", ended_reason="contact-did-not-answer")
            bericht["call_ended"] = r.json()
            return bericht
        else:
            raise ValueError(f"Unbekannte Persona: {persona}")
        bericht["call_ended"] = self.call_ended(call_id, ext, tel, outcomes).json()
        return bericht
