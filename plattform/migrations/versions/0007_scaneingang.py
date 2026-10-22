"""Scan-Eingang: Importkennung am Beleg (Dublettenschutz für Scan-to-Mail, Eingangsordner, Kamera-Scan)

Revision: 0007 (Vorgänger: 0006)
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("beleg") as b:
        b.add_column(sa.Column("import_kennung", sa.String(length=200), nullable=True))
        b.create_unique_constraint("uq_beleg_import_kennung", ["mandant_id", "import_kennung"])


def downgrade() -> None:
    with op.batch_alter_table("beleg") as b:
        b.drop_constraint("uq_beleg_import_kennung", type_="unique")
        b.drop_column("import_kennung")
