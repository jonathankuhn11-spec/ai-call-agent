"""Generischer Webhook für Fachsoftware (openHandwerk, pds, TopKontor …) oder einen n8n-Workflow.

Solange die Schreib-APIs der Fachsoftware nicht geklärt sind, ist dies der Anschlusspunkt: der
Core sendet jede Fachänderung als signiertes JSON (HMAC-SHA256 über den Body, Header
X-SHK-Signatur) an eine URL; dahinter übersetzt n8n oder ein kleiner Dienst in die jeweilige API.
Idempotenz: Header X-SHK-Idempotenz trägt den Schlüssel der Outbox-Nachricht.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
from sqlalchemy.orm import Session

from core.adapter.basis import Konfigurationsfehler, antwort_pruefen, client, kunde_laden, termin_laden
from core.domaene import als_utc
from core.modelle import OutboxEintrag


class WebhookAdapter:
    name = "webhook"

    def __init__(self, konfig: dict[str, Any], transport: httpx.BaseTransport | None = None):
        if not konfig.get("url"):
            raise Konfigurationsfehler("Webhook: url fehlt")
        self.url = konfig["url"]
        self.schluessel = (konfig.get("signatur_schluessel") or "").encode()
        kopf = {"Content-Type": "application/json"}
        if konfig.get("api_key"):
            kopf["Authorization"] = f"Bearer {konfig['api_key']}"
        self.http = client("", kopf, transport)

    def _inhalt(self, e: OutboxEintrag, s: Session) -> dict[str, Any]:
        daten: dict[str, Any] = {"aktion": e.aktion, "mandant_id": e.mandant_id, "payload": e.payload}
        if "termin_id" in e.payload:
            t, fall, kunde = termin_laden(s, e)
            daten["termin"] = {"id": t.id, "beginn": als_utc(t.beginn).isoformat(), "ende": als_utc(t.ende).isoformat(),
                               "status": t.status, "monteur": t.ressource.name,
                               "servicefall": {"id": fall.id, "anliegen": fall.anliegen, "dringlichkeit": fall.dringlichkeit,
                                               "status": fall.status}}
            daten["kunde"] = {"id": kunde.id, "name": kunde.name, "telefon": kunde.telefon, "email": kunde.email,
                              "strasse": kunde.strasse, "plz": kunde.plz, "ort": kunde.ort}
        elif (kunde := kunde_laden(s, e)) is not None:
            daten["kunde"] = {"id": kunde.id, "name": kunde.name, "telefon": kunde.telefon, "email": kunde.email,
                              "strasse": kunde.strasse, "plz": kunde.plz, "ort": kunde.ort}
        return daten

    def zustellen(self, e: OutboxEintrag, s: Session) -> str | None:
        body = json.dumps(self._inhalt(e, s), ensure_ascii=False, sort_keys=True).encode()
        kopf = {"X-SHK-Idempotenz": e.idempotenz_schluessel, "X-SHK-Aktion": e.aktion}
        if self.schluessel:
            kopf["X-SHK-Signatur"] = "sha256=" + hmac.new(self.schluessel, body, hashlib.sha256).hexdigest()
        antwort = antwort_pruefen(self.http.post(self.url, content=body, headers=kopf), "Webhook")
        return str(antwort.get("id") or antwort.get("externe_id") or "") or None
