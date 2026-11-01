"""Runde 12: Stimmprofil je Mandant (geklonte Betreiberstimme für den Sprachagenten, Einwilligung, Referenzaufnahme).

Revision ID: 0015
Revises: 0014
"""
import sqlalchemy as sa
from alembic import op

import core.db

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

AKTUELL = "current_setting('app.mandant', true)"


def upgrade() -> None:
    op.create_table(
        "stimmprofil",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("mandant_id", sa.String(32), sa.ForeignKey("mandant.id"), nullable=False, index=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("rolle", sa.String(20), nullable=False, server_default="inhaber"),
        sa.Column("status", sa.String(20), nullable=False, server_default="bereit"),
        sa.Column("aktiv", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("anbieter", sa.String(20), nullable=False, server_default="eigen"),
        sa.Column("extern_id", sa.String(120), nullable=True),
        sa.Column("einwilligung_am", sa.Date(), nullable=True),
        sa.Column("einwilligung_text", sa.Text(), nullable=True),
        sa.Column("einwilligung_durch", sa.String(120), nullable=True),
        sa.Column("probe_name", sa.String(200), nullable=True),
        sa.Column("probe_mime", sa.String(60), nullable=True),
        sa.Column("probe_groesse", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("probe_dauer_sek", sa.Float(), nullable=True),
        sa.Column("probe", sa.LargeBinary(), nullable=True),
        sa.Column("synchronisiert", core.db.UtcZeit(timezone=True), nullable=True),
        sa.Column("widerrufen_am", core.db.UtcZeit(timezone=True), nullable=True),
        sa.Column("letzter_fehler", sa.String(300), nullable=True),
        sa.Column("erstellt", core.db.UtcZeit(timezone=True), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE stimmprofil ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE stimmprofil FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY mandantentrennung ON stimmprofil USING (mandant_id = {AKTUELL}) WITH CHECK (mandant_id = {AKTUELL})")
        # Rechte kommen aus den DEFAULT PRIVILEGES der Migration 0002


def downgrade() -> None:
    op.drop_table("stimmprofil")
