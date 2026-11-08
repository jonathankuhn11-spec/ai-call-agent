"""Runde 13: Bewerbungen über die Karriereseite der Betriebswebsite (Kurzbewerbung, Talentpool, Löschfristen).

Revision ID: 0016
Revises: 0015
"""
import sqlalchemy as sa
from alembic import op

import core.db

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

AKTUELL = "current_setting('app.mandant', true)"


def upgrade() -> None:
    op.create_table(
        "bewerbung",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("mandant_id", sa.String(32), sa.ForeignKey("mandant.id"), nullable=False, index=True),
        sa.Column("stelle_id", sa.String(40), nullable=True),
        sa.Column("stelle_titel", sa.String(200), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("telefon", sa.String(40), nullable=True),
        sa.Column("email", sa.String(200), nullable=True),
        sa.Column("nachricht", sa.Text(), nullable=True),
        sa.Column("fruehester_start", sa.String(60), nullable=True),
        sa.Column("quelle", sa.String(20), nullable=False, server_default="website"),
        sa.Column("status", sa.String(20), nullable=False, server_default="neu"),
        sa.Column("talentpool", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notiz", sa.Text(), nullable=True),
        sa.Column("externe_id", sa.String(64), nullable=False),
        sa.Column("eingegangen", core.db.UtcZeit(timezone=True), nullable=False),
        sa.Column("abgeschlossen", core.db.UtcZeit(timezone=True), nullable=True),
        sa.UniqueConstraint("mandant_id", "externe_id"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE bewerbung ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE bewerbung FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY mandantentrennung ON bewerbung USING (mandant_id = {AKTUELL}) WITH CHECK (mandant_id = {AKTUELL})")


def downgrade() -> None:
    op.drop_table("bewerbung")
