"""Outbox-Worker: stellt Einträge über Adapter zu (Ports and Adapters, Zustellgarantie).

Jeder Adapter implementiert `zustellen(eintrag) -> externe_id | None` und meldet Fehler per
Exception. Wiederholung mit exponentiellem Backoff; nach MAX_VERSUCHE landet der Eintrag als
„tot“ in der Dead-Letter-Sicht (GET /outbox?status=tot) und erzeugt ein Alarm-Ereignis.
Fehlende Fähigkeiten eines Zielsystems werden explizit degradiert (CsvAdapter, Büro-Aufgabe),
statt still zu scheitern.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.domaene import als_utc, protokollieren
from core.modelle import ExternalRef, OutboxEintrag, jetzt

MAX_VERSUCHE = 6
BASIS_WARTEZEIT = timedelta(seconds=30)


class Adapter(Protocol):
    name: str

    def zustellen(self, eintrag: OutboxEintrag, s: Session) -> str | None: ...


# Entweder feste Zuordnung Ziel -> Adapter (Tests, Demo) oder Auflösung je Nachricht (core.adapter.Register)
Zuordnung = dict[str, Adapter] | Callable[[Session, OutboxEintrag], Adapter]


class ProtokollAdapter:
    """Attrappe für Demo und Tests: merkt sich alle Zustellungen."""

    def __init__(self, name: str):
        self.name, self.zugestellt = name, []

    def zustellen(self, eintrag: OutboxEintrag, s: Session | None = None) -> str | None:
        self.zugestellt.append((eintrag.aktion, eintrag.payload))
        return f"{self.name}-{eintrag.id}"


class CsvAdapter:
    """Degradierter Konnektor für Fachsoftware ohne Schreib-API: CSV zum Import durch das Büro."""

    def __init__(self, name: str, verzeichnis: Path):
        self.name, self.verzeichnis = name, verzeichnis

    def zustellen(self, eintrag: OutboxEintrag, s: Session | None = None) -> str | None:
        self.verzeichnis.mkdir(parents=True, exist_ok=True)
        datei = self.verzeichnis / f"{eintrag.mandant_id}_{eintrag.aktion}.csv"
        neu = not datei.exists()
        with datei.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            if neu:
                w.writerow(["idempotenz_schluessel", "aktion", "payload"])
            w.writerow([eintrag.idempotenz_schluessel, eintrag.aktion, json.dumps(eintrag.payload, ensure_ascii=False)])
        return None


def abarbeiten(s: Session, adapter: Zuordnung, zeitpunkt: datetime | None = None, limit: int = 100) -> dict[str, int]:
    zeitpunkt = zeitpunkt or jetzt()
    zaehler = {"zugestellt": 0, "wiederholung": 0, "tot": 0, "ohne_adapter": 0, "ohne_kanal": 0}
    faellig = s.scalars(select(OutboxEintrag).where(OutboxEintrag.status == "ausstehend")
                        .order_by(OutboxEintrag.id).limit(limit)).all()
    for e in faellig:
        if als_utc(e.naechster_versuch) > als_utc(zeitpunkt):
            continue
        try:
            a = adapter.get(e.ziel) if isinstance(adapter, dict) else adapter(s, e)
            if a is None:
                zaehler["ohne_adapter"] += 1
                continue
            externe_id = a.zustellen(e, s)
        except Exception as fehler:  # noqa: BLE001  Adapterfehler jeder Art führen zur Wiederholung
            e.versuche += 1
            e.letzter_fehler = f"{type(fehler).__name__}: {fehler}"
            if e.versuche >= MAX_VERSUCHE:
                e.status = "tot"
                zaehler["tot"] += 1
                protokollieren(s, e.mandant_id, "outbox.tot", "outbox", str(e.id), "system",
                               {"ziel": e.ziel, "aktion": e.aktion, "fehler": e.letzter_fehler})
            else:
                e.naechster_versuch = als_utc(zeitpunkt) + BASIS_WARTEZEIT * (2 ** (e.versuche - 1))
                zaehler["wiederholung"] += 1
            continue
        e.versuche += 1
        if externe_id is None and e.ziel == "endkunde":
            # kein erreichbarer Kanal (keine E-Mail, keine SMS-Einwilligung): Büro-Aufgabe statt stiller Verlust
            e.status = "ohne_kanal"
            zaehler["ohne_kanal"] += 1
            protokollieren(s, e.mandant_id, "outbox.ohne_kanal", "outbox", str(e.id), "system",
                           {"aktion": e.aktion, "hinweis": "Kunde telefonisch oder per Post informieren"})
            continue
        e.status = "zugestellt"
        zaehler["zugestellt"] += 1
        if externe_id and "termin_id" in e.payload:
            _referenz_merken(s, e.mandant_id, "termin", e.payload["termin_id"], a.name, externe_id)
    s.commit()
    return zaehler


def _referenz_merken(s: Session, mandant_id: str, entitaet: str, entitaet_id: str, zielsystem: str, externe_id: str) -> None:
    ref = s.scalar(select(ExternalRef).where(
        ExternalRef.mandant_id == mandant_id, ExternalRef.entitaet == entitaet,
        ExternalRef.entitaet_id == entitaet_id, ExternalRef.zielsystem == zielsystem))
    if ref is None:
        s.add(ExternalRef(mandant_id=mandant_id, entitaet=entitaet, entitaet_id=entitaet_id,
                          zielsystem=zielsystem, externe_id=externe_id))
    else:
        ref.externe_id, ref.letzter_sync = externe_id, jetzt()
