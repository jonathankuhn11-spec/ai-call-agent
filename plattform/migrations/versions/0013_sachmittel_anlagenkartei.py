"""Runde 7: Sachmittel-Ressourcen, Anlagenkartei, Sachmittel je Termin/Vorgang/Leistung.

Revision ID: 0013
Revises: 0012
"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ressource") as b:
        b.add_column(sa.Column("art", sa.String(20), nullable=False, server_default="person"))
        b.add_column(sa.Column("sachmittel_typ", sa.String(30), nullable=True))
    with op.batch_alter_table("anlage") as b:
        b.add_column(sa.Column("merkmale", sa.JSON(), nullable=False, server_default="{}"))
    with op.batch_alter_table("servicefall") as b:
        b.add_column(sa.Column("sachmittel", sa.String(30), nullable=True))
    with op.batch_alter_table("artikel") as b:
        b.add_column(sa.Column("sachmittel", sa.String(30), nullable=True))
    with op.batch_alter_table("termin") as b:
        b.add_column(sa.Column("sachmittel_id", sa.String(32), sa.ForeignKey("ressource.id"), nullable=True))
        b.create_index("ix_termin_sachmittel_id", ["sachmittel_id"])


def downgrade() -> None:
    with op.batch_alter_table("termin") as b:
        b.drop_index("ix_termin_sachmittel_id")
        b.drop_column("sachmittel_id")
    with op.batch_alter_table("artikel") as b:
        b.drop_column("sachmittel")
    with op.batch_alter_table("servicefall") as b:
        b.drop_column("sachmittel")
    with op.batch_alter_table("anlage") as b:
        b.drop_column("merkmale")
    with op.batch_alter_table("ressource") as b:
        b.drop_column("sachmittel_typ")
        b.drop_column("art")
