"""Lagerbestand je Artikel und Lagerort (zentral, Fahrzeug) für Bestellvorschlag und Rapport-Abbuchung

Revision: 0009 (Vorgänger: 0008)
"""
from alembic import op
import sqlalchemy as sa

import core.db

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bestand",
        sa.Column("mandant_id", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("artikel_id", sa.String(length=32), nullable=False),
        sa.Column("lager", sa.String(length=32), nullable=False),
        sa.Column("menge", sa.Float(), nullable=False),
        sa.Column("mindestbestand", sa.Float(), nullable=False),
        sa.Column("geaendert", core.db.UtcZeit(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["artikel_id"], ["artikel.id"]),
        sa.ForeignKeyConstraint(["mandant_id"], ["mandant.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mandant_id", "artikel_id", "lager"),
    )
    op.create_index("ix_bestand_artikel_id", "bestand", ["artikel_id"])
    op.create_index("ix_bestand_mandant_id", "bestand", ["mandant_id"])
    if op.get_bind().dialect.name == "postgresql":  # Row-Level Security wie bei den übrigen Fachtabellen (0002)
        op.execute("ALTER TABLE bestand ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE bestand FORCE ROW LEVEL SECURITY")
        aktuell = "current_setting('app.mandant', true)"
        op.execute(f"CREATE POLICY mandantentrennung ON bestand USING (mandant_id = {aktuell}) WITH CHECK (mandant_id = {aktuell})")


def downgrade() -> None:
    op.drop_index("ix_bestand_mandant_id", table_name="bestand")
    op.drop_index("ix_bestand_artikel_id", table_name="bestand")
    op.drop_table("bestand")
