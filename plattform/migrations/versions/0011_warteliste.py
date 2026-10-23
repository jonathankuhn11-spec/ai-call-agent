"""Warteliste mit automatischer Nachbesetzung

Revision: 0011 (Vorgänger: 0010)
"""
from alembic import op
import sqlalchemy as sa

import core.db

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wartelisteneintrag",
        sa.Column("mandant_id", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("servicefall_id", sa.String(length=32), nullable=False),
        sa.Column("kunde_id", sa.String(length=32), nullable=False),
        sa.Column("wunsch_von", sa.Date(), nullable=False),
        sa.Column("wunsch_bis", sa.Date(), nullable=False),
        sa.Column("tageszeiten", sa.JSON(), nullable=False),
        sa.Column("wochentage", sa.JSON(), nullable=False),
        sa.Column("dauer_minuten", sa.Integer(), nullable=False),
        sa.Column("qualifikation", sa.String(length=50), nullable=True),
        sa.Column("prioritaet", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("angebot", sa.JSON(), nullable=True),
        sa.Column("angeboten_am", core.db.UtcZeit(timezone=True), nullable=True),
        sa.Column("angebote_anzahl", sa.Integer(), nullable=False),
        sa.Column("notiz", sa.String(length=300), nullable=True),
        sa.Column("erstellt", core.db.UtcZeit(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["kunde_id"], ["kunde.id"]),
        sa.ForeignKeyConstraint(["mandant_id"], ["mandant.id"]),
        sa.ForeignKeyConstraint(["servicefall_id"], ["servicefall.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_wartelisteneintrag_mandant_id", "wartelisteneintrag", ["mandant_id"])
    op.create_index("ix_wartelisteneintrag_servicefall_id", "wartelisteneintrag", ["servicefall_id"])
    op.create_index("ix_wartelisteneintrag_kunde_id", "wartelisteneintrag", ["kunde_id"])
    if op.get_bind().dialect.name == "postgresql":
        aktuell = "current_setting('app.mandant', true)"
        op.execute("ALTER TABLE wartelisteneintrag ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE wartelisteneintrag FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY mandantentrennung ON wartelisteneintrag USING (mandant_id = {aktuell}) WITH CHECK (mandant_id = {aktuell})")


def downgrade() -> None:
    op.drop_index("ix_wartelisteneintrag_kunde_id", table_name="wartelisteneintrag")
    op.drop_index("ix_wartelisteneintrag_servicefall_id", table_name="wartelisteneintrag")
    op.drop_index("ix_wartelisteneintrag_mandant_id", table_name="wartelisteneintrag")
    op.drop_table("wartelisteneintrag")
