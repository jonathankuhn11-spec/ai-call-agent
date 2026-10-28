"""WhatsApp an Endkunden über die Meta WhatsApp Business Cloud API – Terminbestätigung, Nachrichten, Rückkanal.

Nur mit dokumentierter Einwilligung (Kunde.einwilligungen["whatsapp"]); ohne Einwilligung wird nichts gesendet
(None → nächster Kanal). Vom Betrieb begonnene Gespräche verlangen eine von Meta freigegebene Vorlage
(Template); innerhalb von 24 Stunden nach einer Kundennachricht ist Freitext erlaubt. Deshalb: ist eine
Vorlage konfiguriert, wird sie genutzt (Parameter = Text), sonst Freitext.
API: POST https://graph.facebook.com/v21.0/{phone_number_id}/messages, Bearer-Token; Webhook mit Verify-Token.
Konfiguration: phone_number_id, token, verify_token, vorlage_termin, vorlage_nachricht, sprache (de).
"""
from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy.orm import Session

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

BASIS = "https://graph.facebook.com/v21.0"


def wa_nummer(telefon: str | None) -> str | None:
    """E.164 ohne Plus (Meta-Format); deutsche Nummern ohne Landesvorwahl werden ergänzt."""
    if not telefon:
        return None
    ziffern = "".join(c for c in telefon if c.isdigit() or c == "+")
    if ziffern.startswith("+"):
        return ziffern[1:]
    if ziffern.startswith("00"):
        return ziffern[2:]
    if ziffern.startswith("0"):
        return "49" + ziffern[1:]
    return ziffern or None


class WhatsAppAdapter:
    name = "whatsapp"

    def __init__(self, konfig: dict[str, Any], transport: httpx.BaseTransport | None = None):
        if not konfig.get("phone_number_id") or not konfig.get("token"):
            raise Konfigurationsfehler("WhatsApp: phone_number_id und token sind Pflicht")
        self.k = konfig
        self.http = client(konfig.get("basis_url") or BASIS, {"Authorization": f"Bearer {konfig['token']}", "Content-Type": "application/json"}, transport)

    def _senden(self, an: str, text: str, vorlage: str | None) -> str:
        if vorlage:
            inhalt = {"type": "template", "template": {"name": vorlage, "language": {"code": self.k.get("sprache") or "de"},
                                                      "components": [{"type": "body", "parameters": [{"type": "text", "text": text[:1024]}]}]}}
        else:
            inhalt = {"type": "text", "text": {"preview_url": False, "body": text[:4096]}}
        r = self.http.post(f"/{self.k['phone_number_id']}/messages", json={"messaging_product": "whatsapp", "recipient_type": "individual", "to": an, **inhalt})
        antwort = antwort_pruefen(r, "WhatsApp Cloud API")
        nachrichten = antwort.get("messages") or [{}]
        return str(nachrichten[0].get("id") or an)

    def text_senden(self, telefon: str, text: str) -> str:
        """Freitext innerhalb des 24-Stunden-Fensters (Antwort auf eine Kundennachricht)."""
        an = wa_nummer(telefon)
        if not an:
            raise Konfigurationsfehler("WhatsApp: keine gültige Rufnummer")
        return self._senden(an, text, None)

    def zustellen(self, e: OutboxEintrag, s: Session) -> str | None:
        m = s.get(Mandant, e.mandant_id)
        if e.aktion == "terminbestaetigung":
            t, fall, kunde = termin_laden(s, e)
            an = wa_nummer(kunde.telefon)
            if not an or not kunde.einwilligungen.get("whatsapp"):
                return None
            beginn = als_utc(t.beginn).astimezone(ZEITZONE)
            text = (f"{m.name}: Ihr Termin am {beginn.strftime('%d.%m.%Y')} um {beginn.strftime('%H:%M')} Uhr ({fall.anliegen[:80]}) ist bestätigt. "
                    f"Antworten Sie einfach auf diese Nachricht, wenn Sie Fragen haben.")
            return self._senden(an, text, self.k.get("vorlage_termin"))
        if e.aktion == "nachricht":
            kunde = kunde_laden(s, e)
            if kunde is None or not kunde.einwilligungen.get("whatsapp"):
                return None
            an = wa_nummer(kunde.telefon)
            if not an:
                return None
            return self._senden(an, f"{m.name}: {e.payload.get('text', '')}", self.k.get("vorlage_nachricht"))
        return None  # Rechnungen gehen per E-Mail oder Portal

    def pruefen(self) -> str:
        r = self.http.get(f"/{self.k['phone_number_id']}", params={"fields": "display_phone_number,verified_name,quality_rating"})
        a = antwort_pruefen(r, "WhatsApp Cloud API")
        return f"Verbunden: {a.get('verified_name') or ''} {a.get('display_phone_number') or ''} (Qualität {a.get('quality_rating') or '–'})".strip()


def eingang_lesen(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Eingehende Textnachrichten aus dem Meta-Webhook-Format; Statusmeldungen (sent/delivered) werden ignoriert."""
    aus: list[dict[str, Any]] = []
    if payload.get("object") != "whatsapp_business_account":
        return aus
    for eintrag in payload.get("entry") or []:
        for aenderung in eintrag.get("changes") or []:
            wert = aenderung.get("value") or {}
            namen = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in wert.get("contacts") or []}
            for n in wert.get("messages") or []:
                text = (n.get("text") or {}).get("body") if n.get("type") == "text" else None
                if n.get("type") in ("image", "document", "audio", "video") and not text:
                    text = f"[{n['type']} empfangen – bitte im WhatsApp-Konto ansehen]"
                if not text:
                    continue
                aus.append({"id": n.get("id"), "von": n.get("from"), "name": namen.get(n.get("from")), "text": text,
                            "phone_number_id": (wert.get("metadata") or {}).get("phone_number_id")})
    return aus
