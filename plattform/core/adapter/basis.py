"""Gemeinsame Bausteine der Adapter: HTTP-Client mit Zeitlimit, Fehlerklassen, Hilfen.

Adapter sind idempotent zu halten: dieselbe Outbox-Nachricht darf mehrfach zugestellt werden, ohne
doppelte Wirkung (Idempotenzschlüssel als externe Referenz oder Prüfung vor dem Anlegen).
"""
from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.modelle import Anhang, Beleg, ExternalRef, Kunde, Mandant, OutboxEintrag, Rechnung, Servicefall, Termin

ZEITLIMIT = httpx.Timeout(15.0, connect=5.0)


class AdapterFehler(Exception):
    """Vorübergehender Fehler: der Worker wiederholt mit Backoff."""


class Konfigurationsfehler(AdapterFehler):
    """Dauerhafter Fehler (fehlender Schlüssel, falsche URL): landet nach MAX_VERSUCHE im Dead-Letter."""


def client(basis_url: str, kopf: dict[str, str], transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(base_url=basis_url, headers=kopf, timeout=ZEITLIMIT, transport=transport)


def antwort_pruefen(r: httpx.Response, was: str) -> dict[str, Any]:
    if r.status_code in (401, 403):
        raise Konfigurationsfehler(f"{was}: Zugang abgewiesen (HTTP {r.status_code})")
    if r.status_code >= 400:
        raise AdapterFehler(f"{was}: HTTP {r.status_code} {r.text[:300]}")
    try:
        return r.json() if r.content else {}
    except ValueError as e:
        raise AdapterFehler(f"{was}: keine JSON-Antwort") from e


def rechnung_laden(s: Session, e: OutboxEintrag) -> tuple[Rechnung, Mandant]:
    r = s.get(Rechnung, e.payload.get("rechnung_id", ""))
    if r is None or r.mandant_id != e.mandant_id:
        raise Konfigurationsfehler("Rechnung zur Outbox-Nachricht nicht gefunden")
    return r, s.get(Mandant, e.mandant_id)


def termin_laden(s: Session, e: OutboxEintrag) -> tuple[Termin, Servicefall, Kunde]:
    t = s.get(Termin, e.payload.get("termin_id", ""))
    if t is None or t.mandant_id != e.mandant_id:
        raise Konfigurationsfehler("Termin zur Outbox-Nachricht nicht gefunden")
    return t, t.servicefall, t.servicefall.kunde


def kunde_laden(s: Session, e: OutboxEintrag) -> Kunde | None:
    kunde_id = e.payload.get("kunde_id")
    if not kunde_id and e.payload.get("servicefall_id"):
        fall = s.get(Servicefall, e.payload["servicefall_id"])
        kunde_id = fall.kunde_id if fall else None
    k = s.get(Kunde, kunde_id) if kunde_id else None
    return k if k is not None and k.mandant_id == e.mandant_id else None


def beleg_laden(s: Session, e: OutboxEintrag) -> tuple[Beleg, Anhang | None, Mandant]:
    """Eingangsbeleg samt Originaldatei für die Übergabe an Steuertools."""
    b = s.get(Beleg, e.payload.get("beleg_id", ""))
    if b is None or b.mandant_id != e.mandant_id:
        raise Konfigurationsfehler("Beleg zur Outbox-Nachricht nicht gefunden")
    return b, (s.get(Anhang, b.anhang_id) if b.anhang_id else None), s.get(Mandant, e.mandant_id)


def beleg_referenz(s: Session, b: Beleg, zielsystem: str) -> str | None:
    ref = s.scalar(select(ExternalRef).where(ExternalRef.mandant_id == b.mandant_id, ExternalRef.entitaet == "beleg",
                                             ExternalRef.entitaet_id == b.id, ExternalRef.zielsystem == zielsystem))
    return ref.externe_id if ref else None


def beleg_uebergeben(s: Session, b: Beleg, zielsystem: str, externe_id: str) -> str:
    """Referenz merken und Status fortschreiben; bezahlt bleibt bezahlt."""
    s.add(ExternalRef(mandant_id=b.mandant_id, entitaet="beleg", entitaet_id=b.id, zielsystem=zielsystem, externe_id=externe_id[:120]))
    if b.status == "geprueft":
        b.status = "uebergeben"
    return externe_id


def beleg_dateiname(b: Beleg, a: Anhang | None) -> str:
    endung = a.name.rsplit(".", 1)[-1].lower() if a and "." in a.name else "pdf"
    roh = f"{b.lieferant}_{b.nummer or b.id[:8]}"
    for von, nach in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss")):
        roh = roh.replace(von, nach)
    sicher = "".join(c if c.isalnum() and c.isascii() else "_" for c in roh)[:60].strip("_")  # SMB/Windows-tauglich
    return f"{b.datum.isoformat()}_{sicher}.{endung}"
