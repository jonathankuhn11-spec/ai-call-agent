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
from datetime import timedelta, timezone
from typing import Any, Optional

import httpx

from integrations.modelle import iso_lesen, iso_utc
from integrations.signatur import svix_kopfzeilen, telli_signatur
from integrations.uhr import Uhr


INBOUND_PERSONAS = ("termin_verschieben", "termin_kurzfristig", "termin_bestaetigen", "stoerung", "nicht_verifiziert")


class PlattformSimulator:
    def __init__(self, client: httpx.Client, api_key: str = "", webhook_secret: str = "", tool_secret: str = "",
                 agent_id: str = "agent_demo", uhr: Optional[Uhr] = None):
        self.client, self.api_key, self.webhook_secret, self.tool_secret = client, api_key, webhook_secret, tool_secret
        self.agent_id = agent_id
        self.uhr = uhr or Uhr()                # die Plattform hat dieselbe Uhr wie das Backend (Zeitstempel, Signaturen)
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
                "createdAt": iso_utc(self.uhr()), "updatedAt": iso_utc(self.uhr()),
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
                   ended_reason: str = "agent-ended-call", zeitstempel: Optional[int] = None,
                   direction: str = "outbound") -> httpx.Response:
        jetzt = self.uhr()
        transkript = transkript if transkript is not None else [
            {"role": "agent", "content": "Guten Tag, hier ist Lena, die digitale KI-Assistentin von SonnenWerk Energie."},
            {"role": "user", "content": "Ja, hallo?"},
            {"role": "agent", "toolActivity": "lookup_lead", "toolParameters": {}},
            {"role": "user", "content": "Ja, das Haus gehört mir."},
        ]
        typen = {bool: "boolean", int: "number", float: "number"}
        body = {"event": "call_ended",
                "call": {"call_id": call_id, "attempt": attempt, "loop_id": f"loop_{call_id}",
                         "from_number": telefon if direction == "inbound" else "+4925100000",
                         "to_number": "+4925100000" if direction == "inbound" else telefon,
                         "direction": direction, "external_contact_id": external_id,
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
            kopf.update(svix_kopfzeilen(self.webhook_secret, msg_id, roh,
                                        zeitstempel if zeitstempel is not None else int(self.uhr.sekunden())))
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
            morgen = iso_utc(self.uhr() + timedelta(days=1))
            outcomes = {"ergebnis": "rueckruf_vereinbart", "rueckruf_zeitpunkt": morgen}
        elif persona == "opt_out":
            outcomes = {"ergebnis": "opt_out"}
        elif persona == "behauptet_termin":          # Agent behauptet eine Buchung, die es nicht gab
            outcomes = {"ergebnis": "termin_gebucht"}
        elif persona == "nicht_erreicht":
            r = self.call_ended(call_id, ext, tel, {}, transkript=[], status="voicemail", ended_reason="contact-did-not-answer")
            bericht["call_ended"] = r.json()
            return bericht
        elif persona in INBOUND_PERSONAS:
            return self.inbound_gespraech(lead, persona, bericht)
        else:
            raise ValueError(f"Unbekannte Persona: {persona}")
        bericht["call_ended"] = self.call_ended(call_id, ext, tel, outcomes).json()
        return bericht

    # ------------------------------------------------------------ Inbound-Service (Use Case telko_inbound)
    def inbound_gespraech(self, lead: dict, persona: str, bericht: dict[str, Any]) -> dict[str, Any]:
        """Der Kunde ruft an: Contact Lookup über die Nummer, Verifizierung per Postleitzahl, dann das Anliegen."""
        call_id, ext, tel = bericht["call_id"], str(lead["id"]), lead["telefon"]
        bericht["contact_lookup"] = self.contact_lookup(tel)
        bericht["vor_verifizierung"] = self.tool("lookup_lead", call_id, ext)            # muss verweigert werden
        plz = lead.get("plz") or ""
        if persona == "nicht_verifiziert":
            bericht["versuche"] = [self.tool("verify_customer", call_id, ext, plz="00000") for _ in range(3)]
            bericht["nach_dreimal"] = self.tool("verify_customer", call_id, ext, plz=plz)
            outcomes = {"ergebnis": "nicht_verifiziert", "verifiziert": False}
        else:
            bericht["verify"] = self.tool("verify_customer", call_id, ext, plz=plz)
            bericht["lookup"] = self.tool("lookup_lead", call_id, ext)
            if persona == "termin_verschieben":
                bericht["cancel"] = self.tool("cancel_appointment", call_id, ext, grund="Kunde ist an dem Tag nicht zu Hause")
                slots = self.tool("check_slots", call_id, ext)["freie_termine"]
                bericht["book"] = self.tool("book_appointment", call_id, ext, slot_id=slots[0]["slot_id"], art="vor_ort")
                outcomes = {"ergebnis": "termin_verschoben", "verifiziert": True}
            elif persona == "termin_kurzfristig":                                       # Mindestvorlauf: Code lehnt ab
                bericht["cancel"] = self.tool("cancel_appointment", call_id, ext, grund="morgen passt nicht")
                outcomes = {"ergebnis": "weitergeleitet", "verifiziert": True}
            elif persona == "termin_bestaetigen":
                outcomes = {"ergebnis": "termin_bestaetigt", "verifiziert": True}
            elif persona == "stoerung":
                bericht["ticket"] = self.tool("create_ticket", call_id, ext, kategorie="stoerung_internet",
                                              beschreibung="Kein Internet seit heute Morgen, Router-LED rot")
                outcomes = {"ergebnis": "ticket_erstellt", "ticket_kategorie": "stoerung_internet",
                            "stoerung_seit": "heute Morgen", "verifiziert": True}
            else:
                raise ValueError(f"Unbekannte Persona: {persona}")
        transkript = [{"role": "user", "content": "Hallo, ich rufe wegen meines Technikertermins an."},
                      {"role": "agent", "content": "Guten Tag, hier ist Mia, die digitale KI-Assistentin von FlinkNet."},
                      {"role": "user", "content": f"Meine Postleitzahl ist {plz}."}]
        bericht["call_ended"] = self.call_ended(call_id, ext, tel, outcomes, transkript=transkript, direction="inbound").json()
        return bericht


# ---------------------------------------------------------------- Cal.com-Attrappe (Kalenderdienst)
class CalComAttrappe:
    """Spielt die Cal.com API v2 im Prozess: GET /v2/slots, POST /v2/bookings, POST /v2/bookings/{uid}/cancel.

    Zustand sind freie Zeiten (UTC) und Buchungen. `vergeben()` nimmt eine Zeit von außen weg, so wie ein
    Kollege sie im Kalender belegen würde; `ausfaelle` lässt die nächsten Anfragen mit 503 scheitern.
    Jede Anfrage wird auf Authorization und cal-api-version geprüft und protokolliert.
    """

    SLOTS_VERSION, BOOKINGS_VERSION = "2024-09-04", "2026-02-25"

    def __init__(self, zeiten: list, dauer_min: int = 60, api_key: str = "cal_test_key", event_type_id: int = 42):
        self.frei = {z if z.tzinfo else z.replace(tzinfo=timezone.utc) for z in zeiten}
        self.dauer, self.api_key, self.event_type_id = timedelta(minutes=dauer_min), api_key, event_type_id
        self.buchungen: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.ausfaelle = 0
        self._n = 0

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handler), base_url="https://api.cal.com")

    def kalender(self, zeitzone: str = "Europe/Berlin"):
        from integrations.kalender import CalComKalender
        return CalComKalender(self.api_key, self.event_type_id, zeitzone=zeitzone, client=self.client())

    def vergeben(self, start):
        self.frei.discard(start if start.tzinfo else start.replace(tzinfo=timezone.utc))

    def _fehler(self, status: int, code: str, text: str) -> httpx.Response:
        return httpx.Response(status, json={"status": "error", "error": {"code": code, "message": text}})

    def _handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, dict(request.url.params) if request.method == "GET" else body))
        if request.headers.get("Authorization") != f"Bearer {self.api_key}":
            return self._fehler(401, "UnauthorizedException", "Invalid API key")
        if self.ausfaelle > 0:
            self.ausfaelle -= 1
            return httpx.Response(503, text="Service Unavailable")
        pfad = request.url.path
        if request.method == "GET" and pfad == "/v2/slots":
            if request.headers.get("cal-api-version") != self.SLOTS_VERSION:
                return self._fehler(400, "BadRequestException", "cal-api-version missing or unsupported")
            p = request.url.params
            if int(p.get("eventTypeId", -1)) != self.event_type_id:
                return self._fehler(404, "NotFoundException", "Event type not found")
            von, bis = iso_lesen(p["start"]), iso_lesen(p["end"])
            daten: dict[str, list] = {}
            for z in sorted(self.frei):
                if von <= z <= bis:
                    eintrag = {"start": iso_utc(z)}
                    if p.get("format") == "range":
                        eintrag["end"] = iso_utc(z + self.dauer)
                    daten.setdefault(z.strftime("%Y-%m-%d"), []).append(eintrag)
            return httpx.Response(200, json={"status": "success", "data": daten})
        if request.method == "POST" and pfad == "/v2/bookings":
            if request.headers.get("cal-api-version") != self.BOOKINGS_VERSION:
                return self._fehler(400, "BadRequestException", "cal-api-version missing or unsupported")
            if body.get("eventTypeId") != self.event_type_id or not body.get("attendee", {}).get("name"):
                return self._fehler(400, "BadRequestException", "eventTypeId or attendee invalid")
            start = iso_lesen(body["start"])
            if start not in self.frei:
                return self._fehler(400, "BadRequestException", "User either already has booking at this time or is not available")
            self._n += 1
            uid = f"bk_{self._n:04d}"
            self.frei.discard(start)
            self.buchungen[uid] = {"id": self._n, "uid": uid, "status": "accepted", "start": iso_utc(start),
                                   "end": iso_utc(start + self.dauer), "attendees": [body["attendee"]],
                                   "metadata": body.get("metadata", {}), "eventTypeId": self.event_type_id}
            return httpx.Response(201, json={"status": "success", "data": self.buchungen[uid]})
        if request.method == "POST" and pfad.startswith("/v2/bookings/") and pfad.endswith("/cancel"):
            if request.headers.get("cal-api-version") != self.BOOKINGS_VERSION:
                return self._fehler(400, "BadRequestException", "cal-api-version missing or unsupported")
            uid = pfad.split("/")[3]
            b = self.buchungen.get(uid)
            if b is None:
                return self._fehler(404, "NotFoundException", "Booking not found")
            if b["status"] != "cancelled":
                b["status"], b["cancellationReason"] = "cancelled", (body or {}).get("cancellationReason")
                self.frei.add(iso_lesen(b["start"]))
            return httpx.Response(200, json={"status": "success", "data": b})
        return self._fehler(404, "NotFoundException", f"Cannot {request.method} {pfad}")
