"""Lexware Office (früher lexoffice): Kontakt anlegen, Rechnung bzw. Gutschrift übermitteln.

API-Stand nach unserem Kenntnisstand (vor Inbetriebnahme gegen die aktuelle Dokumentation unter
developers.lexware.io prüfen, Testschlüssel im Lexware-Office-Konto erzeugen):
  Basis https://api.lexware.io/v1, Header Authorization: Bearer <API-Key>
  GET  /contacts?email=…           Kontaktsuche (Dublettenvermeidung)
  POST /contacts                   {version:0, roles:{customer:{}}, company|person, addresses, emailAddresses}
  POST /invoices?finalize=true     Rechnung; ohne finalize als Entwurf
  POST /credit-notes?finalize=true Gutschrift (für Storno)
Die Rechnungsnummer vergibt Lexware Office selbst; unsere Nummer steht im Feld „title“ und in der
Referenz, damit Kanzlei und Kunde beide Nummern zuordnen können. Idempotenz über ExternalRef.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.adapter.basis import Konfigurationsfehler, antwort_pruefen, client, rechnung_laden
from core.modelle import ExternalRef, Kunde, OutboxEintrag, Rechnung


class LexwareAdapter:
    name = "lexware"

    def __init__(self, konfig: dict[str, Any], transport: httpx.BaseTransport | None = None):
        if not konfig.get("api_key"):
            raise Konfigurationsfehler("Lexware Office: api_key fehlt")
        self.entwurf = bool(konfig.get("als_entwurf", True))  # Pilot: erst im Lexware-Konto prüfen, dann festschreiben
        self.http = client(konfig.get("basis_url", "https://api.lexware.io/v1"),
                           {"Authorization": f"Bearer {konfig['api_key']}", "Accept": "application/json",
                            "Content-Type": "application/json"}, transport)

    def _kontakt(self, s: Session, kunde: Kunde) -> str:
        ref = s.scalar(select(ExternalRef).where(ExternalRef.mandant_id == kunde.mandant_id, ExternalRef.entitaet == "kunde",
                                                 ExternalRef.entitaet_id == kunde.id, ExternalRef.zielsystem == self.name))
        if ref:
            return ref.externe_id
        if kunde.email:
            treffer = antwort_pruefen(self.http.get("/contacts", params={"email": kunde.email}), "Lexware Kontaktsuche")
            for k in treffer.get("content", []):
                return k["id"]
        teile = kunde.name.split(" ", 1)
        person = {"person": {"firstName": teile[0], "lastName": teile[1]}} if len(teile) == 2 and not kunde.name.lower().endswith(("gmbh", "kg", "ag", "e.k.")) else {"company": {"name": kunde.name}}
        body: dict[str, Any] = {"version": 0, "roles": {"customer": {}}, **person,
                                "addresses": {"billing": [{"street": kunde.strasse or "", "zip": kunde.plz or "", "city": kunde.ort or "",
                                                           "countryCode": "DE"}]}}
        if kunde.email:
            body["emailAddresses"] = {"business": [kunde.email]}
        if kunde.telefon:
            body["phoneNumbers"] = {"business": [kunde.telefon]}
        antwort = antwort_pruefen(self.http.post("/contacts", json=body), "Lexware Kontakt anlegen")
        s.add(ExternalRef(mandant_id=kunde.mandant_id, entitaet="kunde", entitaet_id=kunde.id, zielsystem=self.name, externe_id=antwort["id"]))
        return antwort["id"]

    def _beleg(self, r: Rechnung, kontakt_id: str, stammdaten: dict[str, Any]) -> dict[str, Any]:
        kleinunternehmer = bool(stammdaten.get("kleinunternehmer"))
        vorzeichen = Decimal("-1") if r.art == "storno" else Decimal("1")
        return {
            "voucherDate": f"{r.datum.isoformat()}T00:00:00.000+01:00",
            "address": {"contactId": kontakt_id},
            "lineItems": [{"type": "custom", "name": p["bezeichnung"], "quantity": float(Decimal(p["menge"]) * vorzeichen),
                           "unitName": p["einheit"],
                           "unitPrice": {"currency": "EUR", "netAmount": float(Decimal(p["einzelpreis_netto"])),
                                         "taxRatePercentage": float(Decimal(p["ust_satz"]))}} for p in r.positionen],
            "totalPrice": {"currency": "EUR"},
            "taxConditions": {"taxType": "vatfree" if kleinunternehmer else "net",
                              **({"taxTypeNote": "Kein Ausweis von Umsatzsteuer, da Kleinunternehmer gemäß § 19 UStG"} if kleinunternehmer else {})},
            "paymentConditions": {"paymentTermLabel": f"Zahlbar bis {r.faellig.strftime('%d.%m.%Y')} ohne Abzug.",
                                  "paymentTermDuration": (r.faellig - r.datum).days},
            "shippingConditions": {"shippingDate": f"{(r.leistungsdatum or r.datum).isoformat()}T00:00:00.000+01:00",
                                   "shippingType": "service"},
            "title": "Stornorechnung" if r.art == "storno" else "Rechnung",
            "introduction": f"Interne Nummer {r.nummer}" + (f" · Vorgang {r.servicefall_id}" if r.servicefall_id else ""),
            "remark": stammdaten.get("rechnungshinweis", "Vielen Dank für Ihren Auftrag."),
        }

    def zustellen(self, e: OutboxEintrag, s: Session) -> str | None:
        if e.aktion != "rechnung_uebermitteln":
            return None  # Termine und Aufträge kennt Lexware Office nicht; dafür Webhook/CSV
        r, m = rechnung_laden(s, e)
        kontakt = self._kontakt(s, r.kunde)
        pfad = "/credit-notes" if r.art == "storno" else "/invoices"
        body = self._beleg(r, kontakt, m.stammdaten)
        if r.art == "storno":
            for z in body["lineItems"]:
                z["quantity"] = abs(z["quantity"])  # Gutschrift trägt positive Mengen
        antwort = antwort_pruefen(self.http.post(pfad, json=body, params={} if self.entwurf else {"finalize": "true"}),
                                  "Lexware Beleg anlegen")
        s.add(ExternalRef(mandant_id=r.mandant_id, entitaet="rechnung", entitaet_id=r.id, zielsystem=self.name, externe_id=antwort["id"]))
        return antwort["id"]
