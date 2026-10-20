"""Datenbankanbindung: PostgreSQL im Betrieb, SQLite in Tests und Demo.

Mandantentrennung erfolgt über die Spalte mandant_id an jeder Fachtabelle; jede Abfrage im
Core-Service läuft über den Mandantenkontext der Anfrage (api.mandant_kontext). Das ist mit
Row-Level Security in PostgreSQL direkt nachrüstbar (Policy auf mandant_id), ohne das Schema
zu ändern. Die Architektur sah ursprünglich ein Schema je Mandant vor; die Spaltenlösung ist
im Pilot einfacher zu migrieren und skaliert über 100 Mandanten hinaus.
"""
from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

STANDARD_URL = "sqlite:///plattform-demo.db"


class Basis(DeclarativeBase):
    pass


def engine_erzeugen(url: str | None = None) -> Engine:
    url = url or os.environ.get("DATABASE_URL", STANDARD_URL)
    if url.startswith("sqlite"):
        # sqlite:// (im Speicher) braucht eine geteilte Verbindung, sonst sieht jede Sitzung eine leere DB
        extra = {"poolclass": StaticPool} if url in ("sqlite://", "sqlite:///:memory:") else {}
        engine = create_engine(url, connect_args={"check_same_thread": False}, **extra)

        @event.listens_for(engine, "connect")
        def _fremdschluessel(verbindung, _):  # SQLite prüft Fremdschlüssel nur auf Anweisung
            verbindung.execute("PRAGMA foreign_keys=ON")

        return engine
    return create_engine(url, pool_pre_ping=True)


class Datenbank:
    def __init__(self, url: str | None = None):
        self.engine = engine_erzeugen(url)
        self.sitzungen = sessionmaker(self.engine, expire_on_commit=False)

    def schema_anlegen(self) -> None:
        from core import modelle  # noqa: F401  (registriert die Tabellen an Basis.metadata)

        Basis.metadata.create_all(self.engine)

    def sitzung(self) -> Iterator[Session]:
        with self.sitzungen() as s:
            yield s
