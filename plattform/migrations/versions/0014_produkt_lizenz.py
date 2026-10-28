"""Runde 10: Lizenz und Testphase je Mandant (Editionen, Lizenzschlüssel, Betreiberkonsole).

Revision ID: 0014
Revises: 0013
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("mandant") as b:
        b.add_column(sa.Column("lizenz", sa.JSON(), nullable=True))
        b.add_column(sa.Column("testphase_bis", sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("mandant") as b:
        b.drop_column("testphase_bis")
        b.drop_column("lizenz")
