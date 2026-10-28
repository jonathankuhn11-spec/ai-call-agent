"""Geokoordinaten an Kunde und Objekt (Kundenkarte im Cockpit, Anfahrt)

Revision: 0008 (Vorgänger: 0007)
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

SPALTEN = ("lat", "lon", "geo_quelle", "geo_stand")


def upgrade() -> None:
    for tabelle in ("kunde", "objekt"):
        with op.batch_alter_table(tabelle) as b:
            b.add_column(sa.Column("lat", sa.Float(), nullable=True))
            b.add_column(sa.Column("lon", sa.Float(), nullable=True))
            b.add_column(sa.Column("geo_quelle", sa.String(length=20), nullable=True))
            b.add_column(sa.Column("geo_stand", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for tabelle in ("kunde", "objekt"):
        with op.batch_alter_table(tabelle) as b:
            for spalte in reversed(SPALTEN):
                b.drop_column(spalte)
