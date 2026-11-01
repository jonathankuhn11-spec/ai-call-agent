"""Betriebsmodule: Anhänge, Belegeingang, Zeiterfassung, Rapport, Bereitschaft, Fristen

Revision: 0005 (Vorgänger: 0004)
"""
from alembic import op
import sqlalchemy as sa

import core.db

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

AKTUELL = "current_setting('app.mandant', true)"
TABELLEN = ("anhang", "beleg", "zeiteintrag", "rapport", "bereitschaft", "frist")


def _mandant() -> list:
    return [sa.Column("mandant_id", sa.String(length=32), nullable=False), sa.ForeignKeyConstraint(["mandant_id"], ["mandant.id"])]


def upgrade() -> None:
    op.create_table(
        "anhang",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("bezug_typ", sa.String(length=20), nullable=False),
        sa.Column("bezug_id", sa.String(length=32), nullable=False),
        sa.Column("art", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("mime", sa.String(length=100), nullable=False),
        sa.Column("groesse", sa.Integer(), nullable=False),
        sa.Column("inhalt", sa.LargeBinary(), nullable=False),
        sa.Column("notiz", sa.String(length=300), nullable=True),
        sa.Column("hochgeladen_von", sa.String(length=100), nullable=True),
        sa.Column("hochgeladen", core.db.UtcZeit(timezone=True), nullable=False),
        *_mandant(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_anhang_bezug", "anhang", ["bezug_typ", "bezug_id"], unique=False)
    op.create_table(
        "beleg",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("lieferant", sa.String(length=200), nullable=False),
        sa.Column("nummer", sa.String(length=80), nullable=True),
        sa.Column("datum", sa.Date(), nullable=False),
        sa.Column("faellig", sa.Date(), nullable=True),
        sa.Column("netto", sa.String(length=20), nullable=False),
        sa.Column("umsatzsteuer", sa.String(length=20), nullable=False),
        sa.Column("brutto", sa.String(length=20), nullable=False),
        sa.Column("ust_satz", sa.String(length=6), nullable=False),
        sa.Column("skonto_prozent", sa.String(length=6), nullable=True),
        sa.Column("skonto_bis", sa.Date(), nullable=True),
        sa.Column("positionen", sa.JSON(), nullable=False),
        sa.Column("kategorie", sa.String(length=30), nullable=False),
        sa.Column("servicefall_id", sa.String(length=32), nullable=True),
        sa.Column("anhang_id", sa.String(length=32), nullable=True),
        sa.Column("quelle", sa.String(length=20), nullable=False),
        sa.Column("erkennung", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("notiz", sa.Text(), nullable=True),
        sa.Column("geprueft_von", sa.String(length=100), nullable=True),
        sa.Column("erstellt", core.db.UtcZeit(timezone=True), nullable=False),
        *_mandant(),
        sa.ForeignKeyConstraint(["servicefall_id"], ["servicefall.id"]),
        sa.ForeignKeyConstraint(["anhang_id"], ["anhang.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_beleg_servicefall_id"), "beleg", ["servicefall_id"], unique=False)
    op.create_table(
        "zeiteintrag",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("ressource_id", sa.String(length=32), nullable=False),
        sa.Column("servicefall_id", sa.String(length=32), nullable=True),
        sa.Column("art", sa.String(length=15), nullable=False),
        sa.Column("beginn", core.db.UtcZeit(timezone=True), nullable=False),
        sa.Column("ende", core.db.UtcZeit(timezone=True), nullable=True),
        sa.Column("notiz", sa.String(length=300), nullable=True),
        sa.Column("quelle", sa.String(length=10), nullable=False),
        sa.Column("erstellt", core.db.UtcZeit(timezone=True), nullable=False),
        *_mandant(),
        sa.ForeignKeyConstraint(["ressource_id"], ["ressource.id"]),
        sa.ForeignKeyConstraint(["servicefall_id"], ["servicefall.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_zeit_ressource_beginn", "zeiteintrag", ["ressource_id", "beginn"], unique=False)
    op.create_index(op.f("ix_zeiteintrag_servicefall_id"), "zeiteintrag", ["servicefall_id"], unique=False)
    op.create_table(
        "rapport",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("servicefall_id", sa.String(length=32), nullable=False),
        sa.Column("termin_id", sa.String(length=32), nullable=True),
        sa.Column("ressource_id", sa.String(length=32), nullable=False),
        sa.Column("nummer", sa.String(length=40), nullable=False),
        sa.Column("taetigkeiten", sa.Text(), nullable=False),
        sa.Column("material", sa.JSON(), nullable=False),
        sa.Column("arbeitszeit_minuten", sa.Integer(), nullable=False),
        sa.Column("fahrzeit_minuten", sa.Integer(), nullable=False),
        sa.Column("ergebnis", sa.String(length=20), nullable=False),
        sa.Column("hinweise_kunde", sa.Text(), nullable=True),
        sa.Column("messwerte", sa.JSON(), nullable=False),
        sa.Column("unterschrift_anhang_id", sa.String(length=32), nullable=True),
        sa.Column("unterzeichnet_von", sa.String(length=200), nullable=True),
        sa.Column("unterzeichnet_am", core.db.UtcZeit(timezone=True), nullable=True),
        sa.Column("erstellt", core.db.UtcZeit(timezone=True), nullable=False),
        *_mandant(),
        sa.ForeignKeyConstraint(["servicefall_id"], ["servicefall.id"]),
        sa.ForeignKeyConstraint(["termin_id"], ["termin.id"]),
        sa.ForeignKeyConstraint(["ressource_id"], ["ressource.id"]),
        sa.ForeignKeyConstraint(["unterschrift_anhang_id"], ["anhang.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_rapport_servicefall_id"), "rapport", ["servicefall_id"], unique=False)
    op.create_table(
        "bereitschaft",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("ressource_id", sa.String(length=32), nullable=False),
        sa.Column("von", core.db.UtcZeit(timezone=True), nullable=False),
        sa.Column("bis", core.db.UtcZeit(timezone=True), nullable=False),
        sa.Column("telefon", sa.String(length=32), nullable=False),
        sa.Column("notiz", sa.String(length=200), nullable=True),
        *_mandant(),
        sa.ForeignKeyConstraint(["ressource_id"], ["ressource.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bereitschaft_zeit", "bereitschaft", ["von", "bis"], unique=False)
    op.create_table(
        "frist",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("bezug_typ", sa.String(length=20), nullable=False),
        sa.Column("bezug_id", sa.String(length=32), nullable=True),
        sa.Column("art", sa.String(length=20), nullable=False),
        sa.Column("bezeichnung", sa.String(length=200), nullable=False),
        sa.Column("faellig", sa.Date(), nullable=False),
        sa.Column("intervall_monate", sa.Integer(), nullable=True),
        sa.Column("erledigt_am", sa.Date(), nullable=True),
        sa.Column("hinweis", sa.String(length=300), nullable=True),
        sa.Column("erstellt", core.db.UtcZeit(timezone=True), nullable=False),
        *_mandant(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_frist_faellig", "frist", ["faellig"], unique=False)
    for t in TABELLEN:
        op.create_index(op.f(f"ix_{t}_mandant_id"), t, ["mandant_id"], unique=False)
    if op.get_bind().dialect.name == "postgresql":
        for t in TABELLEN:
            op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
            op.execute(f"CREATE POLICY mandantentrennung ON {t} USING (mandant_id = {AKTUELL}) WITH CHECK (mandant_id = {AKTUELL})")


def downgrade() -> None:
    for t in reversed(TABELLEN):
        op.drop_table(t)
