"""Rechnungen, Betriebsstammdaten und Anbindungen je Mandant

Revision: 0004 (Vorgänger: 0003)
"""
from alembic import op
import sqlalchemy as sa

import core.db

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

AKTUELL = "current_setting('app.mandant', true)"


def upgrade() -> None:
    op.add_column("mandant", sa.Column("stammdaten", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("mandant", sa.Column("integrationen", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("mandant", sa.Column("rechnungsnummer_zaehler", sa.Integer(), nullable=False, server_default="0"))
    op.create_table(
        "rechnung",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("nummer", sa.String(length=40), nullable=False),
        sa.Column("kunde_id", sa.String(length=32), nullable=False),
        sa.Column("servicefall_id", sa.String(length=32), nullable=True),
        sa.Column("art", sa.String(length=20), nullable=False),
        sa.Column("storno_von_id", sa.String(length=32), nullable=True),
        sa.Column("datum", sa.Date(), nullable=False),
        sa.Column("leistungsdatum", sa.Date(), nullable=True),
        sa.Column("faellig", sa.Date(), nullable=False),
        sa.Column("positionen", sa.JSON(), nullable=False),
        sa.Column("netto", sa.String(length=20), nullable=False),
        sa.Column("umsatzsteuer", sa.String(length=20), nullable=False),
        sa.Column("brutto", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("empfaenger", sa.JSON(), nullable=False),
        sa.Column("leitweg_id", sa.String(length=60), nullable=True),
        sa.Column("erstellt", core.db.UtcZeit(timezone=True), nullable=False),
        sa.Column("mandant_id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["kunde_id"], ["kunde.id"]),
        sa.ForeignKeyConstraint(["mandant_id"], ["mandant.id"]),
        sa.ForeignKeyConstraint(["servicefall_id"], ["servicefall.id"]),
        sa.ForeignKeyConstraint(["storno_von_id"], ["rechnung.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mandant_id", "nummer"),
    )
    op.create_index(op.f("ix_rechnung_kunde_id"), "rechnung", ["kunde_id"], unique=False)
    op.create_index(op.f("ix_rechnung_mandant_id"), "rechnung", ["mandant_id"], unique=False)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE rechnung ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE rechnung FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY mandantentrennung ON rechnung USING (mandant_id = {AKTUELL}) WITH CHECK (mandant_id = {AKTUELL})")
        # Rechte kommen aus den DEFAULT PRIVILEGES der Migration 0002


def downgrade() -> None:
    op.drop_index(op.f("ix_rechnung_mandant_id"), table_name="rechnung")
    op.drop_index(op.f("ix_rechnung_kunde_id"), table_name="rechnung")
    op.drop_table("rechnung")
    op.drop_column("mandant", "rechnungsnummer_zaehler")
    op.drop_column("mandant", "integrationen")
    op.drop_column("mandant", "stammdaten")
