"""Bauteil-Lebenszyklus, Dienstplan, Zählerstände, Qualifikationsablauf und Zeitkonto-Stammdaten

Revision: 0012 (Vorgänger: 0011)
"""
from alembic import op
import sqlalchemy as sa

import core.db

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None
TABELLEN = ("bauteil", "dienst", "zaehlerstand")


def upgrade() -> None:
    with op.batch_alter_table("ressource") as b:
        b.add_column(sa.Column("qualifikationen_ablauf", sa.JSON(), nullable=False, server_default="{}"))
        b.add_column(sa.Column("wochenstunden", sa.Float(), nullable=False, server_default="40"))
        b.add_column(sa.Column("urlaubsanspruch_tage", sa.Integer(), nullable=False, server_default="30"))
    op.create_table(
        "bauteil",
        sa.Column("mandant_id", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("anlage_id", sa.String(length=32), nullable=False),
        sa.Column("art", sa.String(length=50), nullable=False),
        sa.Column("bezeichnung", sa.String(length=200), nullable=False),
        sa.Column("pflicht", sa.Boolean(), nullable=False),
        sa.Column("nutzungsdauer_jahre", sa.Integer(), nullable=True),
        sa.Column("intervall_monate", sa.Integer(), nullable=True),
        sa.Column("einbau", sa.Date(), nullable=True),
        sa.Column("letzte_erneuerung", sa.Date(), nullable=True),
        sa.Column("kosten_schaetzung", sa.String(length=20), nullable=True),
        sa.Column("rechtsgrundlage", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("notiz", sa.String(length=300), nullable=True),
        sa.Column("erstellt", core.db.UtcZeit(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["anlage_id"], ["anlage.id"]),
        sa.ForeignKeyConstraint(["mandant_id"], ["mandant.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bauteil_mandant_id", "bauteil", ["mandant_id"])
    op.create_index("ix_bauteil_anlage_id", "bauteil", ["anlage_id"])
    op.create_table(
        "dienst",
        sa.Column("mandant_id", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("ressource_id", sa.String(length=32), nullable=False),
        sa.Column("datum", sa.Date(), nullable=False),
        sa.Column("beginn", sa.Time(), nullable=False),
        sa.Column("ende", sa.Time(), nullable=False),
        sa.Column("art", sa.String(length=20), nullable=False),
        sa.Column("pause_minuten", sa.Integer(), nullable=False),
        sa.Column("schicht", sa.String(length=20), nullable=True),
        sa.Column("notiz", sa.String(length=200), nullable=True),
        sa.Column("erstellt", core.db.UtcZeit(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["mandant_id"], ["mandant.id"]),
        sa.ForeignKeyConstraint(["ressource_id"], ["ressource.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ressource_id", "datum", "beginn"),
    )
    op.create_index("ix_dienst_mandant_id", "dienst", ["mandant_id"])
    op.create_index("ix_dienst_ressource_id", "dienst", ["ressource_id"])
    op.create_index("ix_dienst_datum", "dienst", ["datum"])
    op.create_table(
        "zaehlerstand",
        sa.Column("mandant_id", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("kunde_id", sa.String(length=32), nullable=False),
        sa.Column("anlage_id", sa.String(length=32), nullable=True),
        sa.Column("art", sa.String(length=30), nullable=False),
        sa.Column("wert", sa.Float(), nullable=False),
        sa.Column("datum", sa.Date(), nullable=False),
        sa.Column("quelle", sa.String(length=20), nullable=False),
        sa.Column("notiz", sa.String(length=200), nullable=True),
        sa.Column("erstellt", core.db.UtcZeit(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["anlage_id"], ["anlage.id"]),
        sa.ForeignKeyConstraint(["kunde_id"], ["kunde.id"]),
        sa.ForeignKeyConstraint(["mandant_id"], ["mandant.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_zaehlerstand_mandant_id", "zaehlerstand", ["mandant_id"])
    op.create_index("ix_zaehlerstand_kunde_id", "zaehlerstand", ["kunde_id"])
    op.create_index("ix_zaehlerstand_anlage_id", "zaehlerstand", ["anlage_id"])
    if op.get_bind().dialect.name == "postgresql":
        aktuell = "current_setting('app.mandant', true)"
        for tabelle in TABELLEN:
            op.execute(f"ALTER TABLE {tabelle} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {tabelle} FORCE ROW LEVEL SECURITY")
            op.execute(f"CREATE POLICY mandantentrennung ON {tabelle} USING (mandant_id = {aktuell}) WITH CHECK (mandant_id = {aktuell})")


def downgrade() -> None:
    for tabelle, indizes in (("zaehlerstand", ("anlage_id", "kunde_id", "mandant_id")), ("dienst", ("datum", "ressource_id", "mandant_id")), ("bauteil", ("anlage_id", "mandant_id"))):
        for spalte in indizes:
            op.drop_index(f"ix_{tabelle}_{spalte}", table_name=tabelle)
        op.drop_table(tabelle)
    with op.batch_alter_table("ressource") as b:
        b.drop_column("urlaubsanspruch_tage")
        b.drop_column("wochenstunden")
        b.drop_column("qualifikationen_ablauf")
