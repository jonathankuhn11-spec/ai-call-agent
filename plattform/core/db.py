"""Datenbankanbindung: PostgreSQL im Betrieb, SQLite in Tests und Demo.

Mandantentrennung erfolgt zweistufig: im Code über die Spalte mandant_id und den
Mandantenkontext der Anfrage (api.mandant_kontext), in PostgreSQL zusätzlich über Row-Level
Security (Migration 0002). Dafür setzt jede Transaktion `app.mandant` auf den Mandanten, der mit
mandant_binden an die Sitzung gebunden wurde. Die Architektur sah ursprünglich ein Schema je
Mandant vor; die Spaltenlösung ist einfacher zu migrieren und skaliert über 100 Mandanten hinaus.

Schema: SQLite (Tests, Demo) per create_all, PostgreSQL ausschließlich über Alembic-Migrationen
(`python -m core migrate`), weil der Core dort ohne DDL-Rechte läuft.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import DateTime, create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

STANDARD_URL = "sqlite:///plattform-demo.db"
WURZEL = Path(__file__).resolve().parent.parent


class Basis(DeclarativeBase):
    pass


class UtcZeit(TypeDecorator):
    """Zeitstempel immer als UTC speichern und zeitzonenbehaftet zurückgeben.

    SQLite kennt keine Zeitzonen und verwirft den Offset; ohne Normalisierung würde 09:00 Berlin
    als 09:00 UTC zurückgelesen. PostgreSQL (timestamptz) verhält sich damit identisch.
    """
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, wert: datetime | None, dialect) -> datetime | None:
        if wert is None:
            return None
        wert = wert.replace(tzinfo=timezone.utc) if wert.tzinfo is None else wert.astimezone(timezone.utc)
        return wert.replace(tzinfo=None) if dialect.name == "sqlite" else wert

    def process_result_value(self, wert: datetime | None, dialect) -> datetime | None:
        if wert is None:
            return None
        return wert.replace(tzinfo=timezone.utc) if wert.tzinfo is None else wert.astimezone(timezone.utc)


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


def _mandant_setzen(sitzung: Session, _transaktion, verbindung) -> None:
    # transaktionslokal: nach Commit oder Rollback ist der Wert weg, eine Verbindung aus dem Pool
    # kann also keinen Mandanten der vorigen Anfrage mitbringen
    verbindung.exec_driver_sql("SELECT set_config('app.mandant', %s, true)", (sitzung.info.get("mandant", ""),))


def mandant_binden(s: Session, mandant_id: str) -> None:
    """Bindet den Mandanten an die Sitzung; gilt für die laufende und jede folgende Transaktion."""
    s.info["mandant"] = mandant_id
    if s.in_transaction() and s.get_bind().dialect.name == "postgresql":
        s.execute(text("SELECT set_config('app.mandant', :m, true)"), {"m": mandant_id})


def migrieren(url: str | None = None, ziel: str = "head") -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(WURZEL / "alembic.ini"))
    cfg.attributes["url"] = url or os.environ.get("MIGRATION_DATABASE_URL") or os.environ.get("DATABASE_URL", STANDARD_URL)
    command.upgrade(cfg, ziel)


class Datenbank:
    def __init__(self, url: str | None = None):
        self.engine = engine_erzeugen(url)
        self.sitzungen = sessionmaker(self.engine, expire_on_commit=False)
        self.postgres = self.engine.dialect.name == "postgresql"
        if self.postgres:
            event.listen(self.sitzungen, "after_begin", _mandant_setzen)

    def schema_anlegen(self) -> None:
        """SQLite: Tabellen direkt anlegen. PostgreSQL: nichts tun, das Schema kommt aus den Migrationen."""
        from core import modelle  # noqa: F401  (registriert die Tabellen an Basis.metadata)

        if not self.postgres:
            Basis.metadata.create_all(self.engine)

    def sitzung(self) -> Iterator[Session]:
        with self.sitzungen() as s:
            yield s
