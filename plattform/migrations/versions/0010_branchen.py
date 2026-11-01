"""Branchenprofile: Leistungen mit Dauer und Qualifikation, Kundenkartei, No-Show-Zähler, Gutscheine

Revision: 0010 (Vorgänger: 0009)
"""
from alembic import op
import sqlalchemy as sa

import core.db

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("artikel") as b:
        b.add_column(sa.Column("dauer_minuten", sa.Integer(), nullable=True))
        b.add_column(sa.Column("qualifikation", sa.String(length=50), nullable=True))
    with op.batch_alter_table("kunde") as b:
        b.add_column(sa.Column("kartei", sa.JSON(), nullable=False, server_default="{}"))
        b.add_column(sa.Column("nicht_erschienen", sa.Integer(), nullable=False, server_default="0"))
    op.create_table(
        "gutschein",
        sa.Column("mandant_id", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("wert", sa.String(length=20), nullable=False),
        sa.Column("restwert", sa.String(length=20), nullable=False),
        sa.Column("kunde_id", sa.String(length=32), nullable=True),
        sa.Column("empfaenger", sa.String(length=200), nullable=True),
        sa.Column("gueltig_bis", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("einloesungen", sa.JSON(), nullable=False),
        sa.Column("ausgestellt", core.db.UtcZeit(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["kunde_id"], ["kunde.id"]),
        sa.ForeignKeyConstraint(["mandant_id"], ["mandant.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mandant_id", "code"),
    )
    op.create_index("ix_gutschein_mandant_id", "gutschein", ["mandant_id"])
    if op.get_bind().dialect.name == "postgresql":
        aktuell = "current_setting('app.mandant', true)"
        op.execute("ALTER TABLE gutschein ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE gutschein FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY mandantentrennung ON gutschein USING (mandant_id = {aktuell}) WITH CHECK (mandant_id = {aktuell})")


def downgrade() -> None:
    op.drop_index("ix_gutschein_mandant_id", table_name="gutschein")
    op.drop_table("gutschein")
    with op.batch_alter_table("kunde") as b:
        b.drop_column("nicht_erschienen")
        b.drop_column("kartei")
    with op.batch_alter_table("artikel") as b:
        b.drop_column("qualifikation")
        b.drop_column("dauer_minuten")
