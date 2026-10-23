"""Alembic-Umgebung: Zielschema ist core.modelle (Basis.metadata)."""
from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

from core import modelle  # noqa: F401  (registriert die Tabellen)
from core.db import Basis

ziel = Basis.metadata
url = context.config.attributes.get("url") or os.environ.get("MIGRATION_DATABASE_URL") or os.environ["DATABASE_URL"]

if context.is_offline_mode():
    context.configure(url=url, target_metadata=ziel, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with create_engine(url).connect() as verbindung:
        context.configure(connection=verbindung, target_metadata=ziel, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
