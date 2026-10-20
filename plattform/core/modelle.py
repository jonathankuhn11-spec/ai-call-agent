"""Kanonisches Datenmodell (Abschnitt „Kanonisches Datenmodell“ der Backend-Architektur).

Das Modell bildet den Betrieb als Digital Twin ab und abstrahiert von der Fachsoftware: CRM
(Kunde, Objekt, Anlage), Disposition (Ressource, Termin), Vorgangssteuerung (Servicefall mit
Statusautomat), Kommunikation, Freigabe, Event-Log, Outbox und ExternalRef. Betriebe ohne
Fachsoftware nutzen es direkt als CRM; Betriebe mit Fachsoftware über Adapter (ExternalRef).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Index, Integer, String, Text, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Basis


def neue_id() -> str:
    return uuid.uuid4().hex


def jetzt() -> datetime:
    return datetime.now(timezone.utc)


class Mandant(Basis):
    __tablename__ = "mandant"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    name: Mapped[str] = mapped_column(String(200))
    gewerk: Mapped[str] = mapped_column(String(50), default="SHK")
    # Freigabe-Policies je Aktion, z. B. {"termin_bestaetigen": "auto"}; Klasse C ist nie automatisierbar
    policies: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Fahrzeitpuffer vor und nach Terminen, Schrittweite der Zeitfenster
    puffer_minuten: Mapped[int] = mapped_column(Integer, default=30)
    raster_minuten: Mapped[int] = mapped_column(Integer, default=30)
    erstellt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=jetzt)


class _Mandantenbezug:
    mandant_id: Mapped[str] = mapped_column(String(32), ForeignKey("mandant.id"), index=True)


class Kunde(_Mandantenbezug, Basis):
    __tablename__ = "kunde"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    name: Mapped[str] = mapped_column(String(200))
    telefon: Mapped[str | None] = mapped_column(String(32), index=True)   # E.164, Dublettenschlüssel
    email: Mapped[str | None] = mapped_column(String(200))
    strasse: Mapped[str | None] = mapped_column(String(200))
    plz: Mapped[str | None] = mapped_column(String(10))
    ort: Mapped[str | None] = mapped_column(String(100))
    # Einwilligungen mit Zeitstempel, z. B. {"aufzeichnung": "2026-10-20T10:00:00Z", "sms": ...}
    einwilligungen: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    vertragsstatus: Mapped[str] = mapped_column(String(30), default="ohne")  # ohne, wartungsvertrag
    notiz: Mapped[str | None] = mapped_column(Text)
    erstellt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=jetzt)

    objekte: Mapped[list[Objekt]] = relationship(back_populates="kunde")


class Objekt(_Mandantenbezug, Basis):
    """Gebäude oder Liegenschaft, an der Anlagen installiert sind."""
    __tablename__ = "objekt"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    kunde_id: Mapped[str] = mapped_column(String(32), ForeignKey("kunde.id"), index=True)
    bezeichnung: Mapped[str] = mapped_column(String(200), default="Hauptobjekt")
    strasse: Mapped[str | None] = mapped_column(String(200))
    plz: Mapped[str | None] = mapped_column(String(10))
    ort: Mapped[str | None] = mapped_column(String(100))
    gebaeudetyp: Mapped[str | None] = mapped_column(String(50))  # Einfamilienhaus, Mehrfamilienhaus, Gewerbe

    kunde: Mapped[Kunde] = relationship(back_populates="objekte")
    anlagen: Mapped[list[Anlage]] = relationship(back_populates="objekt")


class Anlage(_Mandantenbezug, Basis):
    __tablename__ = "anlage"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    objekt_id: Mapped[str] = mapped_column(String(32), ForeignKey("objekt.id"), index=True)
    anlagentyp: Mapped[str] = mapped_column(String(50))  # Schlüssel aus ontologie.ANLAGENTYPEN
    hersteller: Mapped[str | None] = mapped_column(String(100))
    modell: Mapped[str | None] = mapped_column(String(100))
    baujahr: Mapped[int | None] = mapped_column(Integer)
    wartungsintervall_monate: Mapped[int | None] = mapped_column(Integer, default=12)
    letzte_wartung: Mapped[date | None] = mapped_column(Date)

    objekt: Mapped[Objekt] = relationship(back_populates="anlagen")


class Ressource(_Mandantenbezug, Basis):
    """Monteur oder Team; Qualifikationen sind Constraints der Terminvergabe."""
    __tablename__ = "ressource"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    name: Mapped[str] = mapped_column(String(200))
    qualifikationen: Mapped[list[str]] = mapped_column(JSON, default=list)  # z. B. ["gas", "kaelteschein"]
    arbeitstage: Mapped[list[int]] = mapped_column(JSON, default=lambda: [0, 1, 2, 3, 4])  # 0 = Montag
    arbeitsbeginn: Mapped[time] = mapped_column(Time, default=time(7, 30))
    arbeitsende: Mapped[time] = mapped_column(Time, default=time(16, 30))
    aktiv: Mapped[bool] = mapped_column(default=True)


class Servicefall(_Mandantenbezug, Basis):
    __tablename__ = "servicefall"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    kunde_id: Mapped[str] = mapped_column(String(32), ForeignKey("kunde.id"), index=True)
    anlage_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("anlage.id"))
    anliegen: Mapped[str] = mapped_column(Text)
    symptom: Mapped[str | None] = mapped_column(Text)
    dringlichkeit: Mapped[str] = mapped_column(String(20), default="normal")  # notdienst, hoch, normal, niedrig
    status: Mapped[str] = mapped_column(String(30), default="eingegangen")
    quelle: Mapped[str] = mapped_column(String(20), default="manuell")  # telefon, email, web, manuell
    benoetigte_qualifikation: Mapped[str | None] = mapped_column(String(50))
    dauer_minuten: Mapped[int] = mapped_column(Integer, default=90)
    erstellt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=jetzt)
    geaendert: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=jetzt, onupdate=jetzt)

    kunde: Mapped[Kunde] = relationship()
    anlage: Mapped[Anlage | None] = relationship()
    termine: Mapped[list[Termin]] = relationship(back_populates="servicefall")


class Termin(_Mandantenbezug, Basis):
    __tablename__ = "termin"
    __table_args__ = (Index("ix_termin_ressource_zeit", "ressource_id", "beginn"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    servicefall_id: Mapped[str] = mapped_column(String(32), ForeignKey("servicefall.id"), index=True)
    ressource_id: Mapped[str] = mapped_column(String(32), ForeignKey("ressource.id"))
    beginn: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ende: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="vorgeschlagen")  # vorgeschlagen, bestaetigt, abgesagt

    servicefall: Mapped[Servicefall] = relationship(back_populates="termine")
    ressource: Mapped[Ressource] = relationship()


class Kommunikation(_Mandantenbezug, Basis):
    __tablename__ = "kommunikation"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    kunde_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("kunde.id"), index=True)
    servicefall_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("servicefall.id"))
    kanal: Mapped[str] = mapped_column(String(20))  # telefon, email, sms, whatsapp
    richtung: Mapped[str] = mapped_column(String(10), default="eingehend")
    transkript: Mapped[str | None] = mapped_column(Text)
    zusammenfassung: Mapped[str | None] = mapped_column(Text)
    extraktion: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    zeit: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=jetzt)


class Freigabe(_Mandantenbezug, Basis):
    """Eintrag der Freigabe-Inbox (Human-in-the-Loop für Klasse B und C)."""
    __tablename__ = "freigabe"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    aktion: Mapped[str] = mapped_column(String(50))
    risikoklasse: Mapped[str] = mapped_column(String(1))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    servicefall_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("servicefall.id"))
    status: Mapped[str] = mapped_column(String(20), default="offen")  # offen, freigegeben, abgelehnt
    begruendung: Mapped[str | None] = mapped_column(Text)
    erstellt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=jetzt)
    entschieden_von: Mapped[str | None] = mapped_column(String(100))
    entschieden_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Ereignis(_Mandantenbezug, Basis):
    """Append-only Event-Log: Grundlage für Audit, Replay und Nachvollziehbarkeit jeder KI-Entscheidung."""
    __tablename__ = "ereignis"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    typ: Mapped[str] = mapped_column(String(80), index=True)  # z. B. servicefall.eingegangen
    entitaet: Mapped[str] = mapped_column(String(40))
    entitaet_id: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    akteur: Mapped[str] = mapped_column(String(10))  # mensch, ki, system
    akteur_name: Mapped[str | None] = mapped_column(String(100))
    zeit: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=jetzt)


class OutboxEintrag(_Mandantenbezug, Basis):
    """Transactional Outbox: wird in derselben Transaktion wie die Fachänderung geschrieben."""
    __tablename__ = "outbox"
    __table_args__ = (UniqueConstraint("mandant_id", "idempotenz_schluessel"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ziel: Mapped[str] = mapped_column(String(50))     # Adaptername, z. B. fachsoftware, sms, csv
    aktion: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    idempotenz_schluessel: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="ausstehend")  # ausstehend, zugestellt, tot
    versuche: Mapped[int] = mapped_column(Integer, default=0)
    naechster_versuch: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=jetzt)
    letzter_fehler: Mapped[str | None] = mapped_column(Text)


class ExternalRef(_Mandantenbezug, Basis):
    __tablename__ = "external_ref"
    __table_args__ = (UniqueConstraint("mandant_id", "entitaet", "entitaet_id", "zielsystem"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entitaet: Mapped[str] = mapped_column(String(40))
    entitaet_id: Mapped[str] = mapped_column(String(32))
    zielsystem: Mapped[str] = mapped_column(String(50))
    externe_id: Mapped[str] = mapped_column(String(120))
    letzter_sync: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=jetzt)
