"""Ressource: Anmeldename des Monteurs (Zuordnung Monteur zu Einsatz)

Revision: 0003 (Vorgänger: 0002)
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ressource", sa.Column("benutzer", sa.String(length=100), nullable=True))
    op.create_index(op.f("ix_ressource_benutzer"), "ressource", ["benutzer"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_ressource_benutzer"), table_name="ressource")
    op.drop_column("ressource", "benutzer")
