"""sevDesk: Kontakt anlegen und Rechnung übermitteln.

API-Stand nach unserem Kenntnisstand (vor Inbetriebnahme gegen api.sevdesk.de prüfen):
  Basis https://my.sevdesk.de/api/v1, Header Authorization: <API-Token>
  GET  /Contact?name=…                       Kontaktsuche
  POST /Contact                              {name, category:{id:3, objectName:"Category"}}  (3 = Kunde)
  POST /Invoice/Factory/saveInvoice          {invoice:{…}, invoicePosSave:[…]}
Stornos werden als Rechnung mit negativen Mengen übermittelt (sevDesk: „Stornorechnung“ entsteht dort
über die Funktion „Stornieren“; die Kanzlei prüft im Pilot).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.adapter.basis import Konfigurationsfehler, antwort_pruefen, client, rechnung_laden
from core.modelle import ExternalRef, Kunde, OutboxEintrag


class SevdeskAdapter:
    name = "sevdesk"

    def __init__(self, konfig: dict[str, Any], transport: httpx.BaseTransport | None = None):
        if not konfig.get("api_token"):
            raise Konfigurationsfehler("sevDesk: api_token fehlt")
        self.http = client(konfig.get("basis_url", "https://my.sevdesk.de/api/v1"),
                           {"Authorization": konfig["api_token"], "Accept": "application/json"}, transport)
        self.status = "100" if konfig.get("als_entwurf", True) else "200"  # 100 Entwurf, 200 offen

    def _kontakt(self, s: Session, kunde: Kunde) -> str:
        ref = s.scalar(select(ExternalRef).where(ExternalRef.mandant_id == kunde.mandant_id, ExternalRef.entitaet == "kunde",
                                                 ExternalRef.entitaet_id == kunde.id, ExternalRef.zielsystem == self.name))
        if ref:
            return ref.externe_id
        treffer = antwort_pruefen(self.http.get("/Contact", params={"name": kunde.name, "limit": 1}), "sevDesk Kontaktsuche")
        for k in treffer.get("objects", []):
            return str(k["id"])
        body = {"name": kunde.name, "category": {"id": 3, "objectName": "Category"},
                "description": f"Angelegt vom SHK-Backoffice, Kunde {kunde.id}"}
        antwort = antwort_pruefen(self.http.post("/Contact", json=body), "sevDesk Kontakt anlegen")
        kontakt_id = str(antwort["objects"]["id"])
        if kunde.strasse or kunde.ort:
            self.http.post("/ContactAddress", json={"contact": {"id": kontakt_id, "objectName": "Contact"}, "street": kunde.strasse,
                                                    "zip": kunde.plz, "city": kunde.ort, "country": {"id": 1, "objectName": "StaticCountry"},
                                                    "category": {"id": 47, "objectName": "Category"}})
        s.add(ExternalRef(mandant_id=kunde.mandant_id, entitaet="kunde", entitaet_id=kunde.id, zielsystem=self.name, externe_id=kontakt_id))
        return kontakt_id

    def zustellen(self, e: OutboxEintrag, s: Session) -> str | None:
        if e.aktion != "rechnung_uebermitteln":
            return None
        r, m = rechnung_laden(s, e)
        kontakt = self._kontakt(s, r.kunde)
        kleinunternehmer = bool(m.stammdaten.get("kleinunternehmer"))
        body = {
            "invoice": {"objectName": "Invoice", "contact": {"id": kontakt, "objectName": "Contact"},
                        "invoiceDate": r.datum.strftime("%d.%m.%Y"), "deliveryDate": (r.leistungsdatum or r.datum).strftime("%d.%m.%Y"),
                        "header": f"{'Stornorechnung' if r.art == 'storno' else 'Rechnung'} {r.nummer}",
                        "headText": f"Interne Nummer {r.nummer}", "status": self.status, "invoiceType": "RE", "currency": "EUR",
                        "timeToPay": (r.faellig - r.datum).days, "mapAll": True, "discount": 0,
                        "taxType": "noteu" if kleinunternehmer else "default",
                        "taxText": "Kein Ausweis von Umsatzsteuer, da Kleinunternehmer gemäß § 19 UStG" if kleinunternehmer else "Umsatzsteuer",
                        "taxRate": 0 if kleinunternehmer else 19},
            "invoicePosSave": [{"objectName": "InvoicePos", "mapAll": True, "quantity": float(Decimal(p["menge"])),
                                "price": float(Decimal(p["einzelpreis_netto"])), "name": p["bezeichnung"],
                                "unity": {"id": 1, "objectName": "Unity"}, "taxRate": float(Decimal(p["ust_satz"]))} for p in r.positionen],
        }
        antwort = antwort_pruefen(self.http.post("/Invoice/Factory/saveInvoice", json=body), "sevDesk Rechnung anlegen")
        externe_id = str(antwort.get("objects", {}).get("invoice", {}).get("id") or "")
        if not externe_id:
            raise Konfigurationsfehler("sevDesk: Antwort ohne Rechnungs-ID")
        s.add(ExternalRef(mandant_id=r.mandant_id, entitaet="rechnung", entitaet_id=r.id, zielsystem=self.name, externe_id=externe_id))
        return externe_id
