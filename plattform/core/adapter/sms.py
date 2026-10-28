"""SMS an Endkunden über seven.io (deutscher Anbieter, HTTP-API) – Terminbestätigung und Nachricht.

Nur mit dokumentierter Einwilligung des Kunden (Kunde.einwilligungen["sms"]); ohne Einwilligung wird
nichts gesendet und None zurückgegeben (Degradierung auf E-Mail oder Büro-Anruf).
API: POST https://gateway.seven.io/api/sms, Header X-Api-Key, Felder to, text, from.
Andere Anbieter (sipgate, Twilio) lassen sich über denselben Aufbau ergänzen.
"""
from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy.orm import Session

from core import vorlagen
from core.adapter.basis import (
    Konfigurationsfehler,
    antwort_pruefen,
    client,
    kunde_laden,
    termin_laden,
)
from core.disposition import ZEITZONE
from core.domaene import als_utc
from core.modelle import Mandant, OutboxEintrag


class SmsAdapter:
    name = "sms"

    def __init__(self, konfig: dict[str, Any], transport: httpx.BaseTransport | None = None):
        if not konfig.get("api_key"):
            raise Konfigurationsfehler("SMS: api_key fehlt")
        self.absender = konfig.get("absender", "Handwerk")[:11]
        self.http = client(konfig.get("basis_url", "https://gateway.seven.io/api"),
                           {"X-Api-Key": konfig["api_key"], "Accept": "application/json"}, transport)

    def _senden(self, an: str, text: str, referenz: str) -> str:
        r = self.http.post("/sms", data={"to": an, "text": text[:600], "from": self.absender, "foreign_id": referenz})
        antwort = antwort_pruefen(r, "seven.io")
        if str(antwort.get("success")) not in ("100", "101"):
            raise Konfigurationsfehler(f"seven.io: Statuscode {antwort.get('success')}")
        nachrichten = antwort.get("messages") or [{}]
        return str(nachrichten[0].get("id") or referenz)

    def zustellen(self, e: OutboxEintrag, s: Session) -> str | None:
        m = s.get(Mandant, e.mandant_id)
        if e.aktion == "terminbestaetigung":
            t, fall, kunde = termin_laden(s, e)
            if not kunde.telefon or not kunde.einwilligungen.get("sms"):
                return None
            beginn = als_utc(t.beginn).astimezone(ZEITZONE)
            text = vorlagen.rendern(m, "terminbestaetigung_sms", vorlagen.termin_kontext(t, fall, kunde, m, beginn))["text"]
            return self._senden(kunde.telefon, text, e.idempotenz_schluessel)
        if e.aktion == "nachricht":
            kunde = kunde_laden(s, e)
            if kunde is None or not kunde.telefon or not kunde.einwilligungen.get("sms"):
                return None
            return self._senden(kunde.telefon, f"{m.name}: {e.payload.get('text', '')}", e.idempotenz_schluessel)
        return None  # Rechnungen und Belege gehen nie per SMS
