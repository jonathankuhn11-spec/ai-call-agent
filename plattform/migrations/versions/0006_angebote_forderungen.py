"""Angebote, Forderungen, Verträge, Abwesenheiten, Anfragen

Revision: 0006 (Vorgänger: 0005)
"""
from alembic import op
import sqlalchemy as sa

import core.db

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

AKTUELL = "current_setting('app.mandant', true)"
TABELLEN = ("artikel", "abwesenheit", "vertrag", "anfrage", "angebot", "mahnung", "zahlung")


def upgrade() -> None:
    op.create_table('artikel',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('nummer', sa.String(length=40), nullable=False),
    sa.Column('bezeichnung', sa.String(length=200), nullable=False),
    sa.Column('kategorie', sa.String(length=20), nullable=False),
    sa.Column('einheit', sa.String(length=10), nullable=False),
    sa.Column('einkauf_netto', sa.String(length=20), nullable=True),
    sa.Column('verkauf_netto', sa.String(length=20), nullable=False),
    sa.Column('ust_satz', sa.String(length=6), nullable=False),
    sa.Column('lieferant', sa.String(length=200), nullable=True),
    sa.Column('aktiv', sa.Boolean(), nullable=False),
    sa.Column('mandant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('mandant_id', 'nummer')
    )
    op.create_index(op.f('ix_artikel_mandant_id'), 'artikel', ['mandant_id'], unique=False)
    op.create_table('abwesenheit',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('ressource_id', sa.String(length=32), nullable=False),
    sa.Column('von', sa.Date(), nullable=False),
    sa.Column('bis', sa.Date(), nullable=False),
    sa.Column('art', sa.String(length=20), nullable=False),
    sa.Column('notiz', sa.String(length=300), nullable=True),
    sa.Column('mandant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandant.id'], ),
    sa.ForeignKeyConstraint(['ressource_id'], ['ressource.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_abwesenheit_mandant_id'), 'abwesenheit', ['mandant_id'], unique=False)
    op.create_index('ix_abwesenheit_ressource', 'abwesenheit', ['ressource_id', 'von'], unique=False)
    op.create_table('vertrag',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('kunde_id', sa.String(length=32), nullable=False),
    sa.Column('bezeichnung', sa.String(length=200), nullable=False),
    sa.Column('anlagen_ids', sa.JSON(), nullable=False),
    sa.Column('leistungen', sa.Text(), nullable=True),
    sa.Column('beginn', sa.Date(), nullable=False),
    sa.Column('laufzeit_monate', sa.Integer(), nullable=False),
    sa.Column('kuendigungsfrist_monate', sa.Integer(), nullable=False),
    sa.Column('verlaengerung_monate', sa.Integer(), nullable=False),
    sa.Column('preis_jahr_netto', sa.String(length=20), nullable=False),
    sa.Column('ust_satz', sa.String(length=6), nullable=False),
    sa.Column('abrechnung_monate', sa.Integer(), nullable=False),
    sa.Column('naechste_abrechnung', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('gekuendigt_zum', sa.Date(), nullable=True),
    sa.Column('notiz', sa.Text(), nullable=True),
    sa.Column('erstellt', core.db.UtcZeit(timezone=True), nullable=False),
    sa.Column('mandant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['kunde_id'], ['kunde.id'], ),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_vertrag_kunde_id'), 'vertrag', ['kunde_id'], unique=False)
    op.create_index(op.f('ix_vertrag_mandant_id'), 'vertrag', ['mandant_id'], unique=False)
    op.create_table('anfrage',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('kanal', sa.String(length=20), nullable=False),
    sa.Column('externe_id', sa.String(length=200), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=True),
    sa.Column('telefon', sa.String(length=32), nullable=True),
    sa.Column('email', sa.String(length=200), nullable=True),
    sa.Column('betreff', sa.String(length=300), nullable=True),
    sa.Column('nachricht', sa.Text(), nullable=True),
    sa.Column('kunde_id', sa.String(length=32), nullable=True),
    sa.Column('servicefall_id', sa.String(length=32), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('eingegangen', core.db.UtcZeit(timezone=True), nullable=False),
    sa.Column('mandant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['kunde_id'], ['kunde.id'], ),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandant.id'], ),
    sa.ForeignKeyConstraint(['servicefall_id'], ['servicefall.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('mandant_id', 'externe_id')
    )
    op.create_index(op.f('ix_anfrage_mandant_id'), 'anfrage', ['mandant_id'], unique=False)
    op.create_table('angebot',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('nummer', sa.String(length=40), nullable=False),
    sa.Column('kunde_id', sa.String(length=32), nullable=False),
    sa.Column('servicefall_id', sa.String(length=32), nullable=True),
    sa.Column('rechnung_id', sa.String(length=32), nullable=True),
    sa.Column('titel', sa.String(length=200), nullable=False),
    sa.Column('einleitung', sa.Text(), nullable=True),
    sa.Column('datum', sa.Date(), nullable=False),
    sa.Column('gueltig_bis', sa.Date(), nullable=False),
    sa.Column('positionen', sa.JSON(), nullable=False),
    sa.Column('netto', sa.String(length=20), nullable=False),
    sa.Column('umsatzsteuer', sa.String(length=20), nullable=False),
    sa.Column('brutto', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('empfaenger', sa.JSON(), nullable=False),
    sa.Column('entschieden_am', sa.Date(), nullable=True),
    sa.Column('erstellt', core.db.UtcZeit(timezone=True), nullable=False),
    sa.Column('mandant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['kunde_id'], ['kunde.id'], ),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandant.id'], ),
    sa.ForeignKeyConstraint(['rechnung_id'], ['rechnung.id'], ),
    sa.ForeignKeyConstraint(['servicefall_id'], ['servicefall.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('mandant_id', 'nummer')
    )
    op.create_index(op.f('ix_angebot_kunde_id'), 'angebot', ['kunde_id'], unique=False)
    op.create_index(op.f('ix_angebot_mandant_id'), 'angebot', ['mandant_id'], unique=False)
    op.create_table('mahnung',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('rechnung_id', sa.String(length=32), nullable=False),
    sa.Column('stufe', sa.Integer(), nullable=False),
    sa.Column('datum', sa.Date(), nullable=False),
    sa.Column('frist', sa.Date(), nullable=False),
    sa.Column('gebuehr', sa.String(length=20), nullable=False),
    sa.Column('offen', sa.String(length=20), nullable=False),
    sa.Column('erstellt', core.db.UtcZeit(timezone=True), nullable=False),
    sa.Column('mandant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandant.id'], ),
    sa.ForeignKeyConstraint(['rechnung_id'], ['rechnung.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_mahnung_mandant_id'), 'mahnung', ['mandant_id'], unique=False)
    op.create_index(op.f('ix_mahnung_rechnung_id'), 'mahnung', ['rechnung_id'], unique=False)
    op.create_table('zahlung',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('rechnung_id', sa.String(length=32), nullable=False),
    sa.Column('datum', sa.Date(), nullable=False),
    sa.Column('betrag', sa.String(length=20), nullable=False),
    sa.Column('art', sa.String(length=20), nullable=False),
    sa.Column('referenz', sa.String(length=200), nullable=True),
    sa.Column('quelle', sa.String(length=20), nullable=False),
    sa.Column('erstellt', core.db.UtcZeit(timezone=True), nullable=False),
    sa.Column('mandant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandant.id'], ),
    sa.ForeignKeyConstraint(['rechnung_id'], ['rechnung.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_zahlung_mandant_id'), 'zahlung', ['mandant_id'], unique=False)
    op.create_index(op.f('ix_zahlung_rechnung_id'), 'zahlung', ['rechnung_id'], unique=False)
    op.add_column('mandant', sa.Column('angebotsnummer_zaehler', sa.Integer(), nullable=False, server_default="0"))
    if op.get_bind().dialect.name == "postgresql":
        for t in TABELLEN:
            op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
            op.execute(f"CREATE POLICY mandantentrennung ON {t} USING (mandant_id = {AKTUELL}) WITH CHECK (mandant_id = {AKTUELL})")


def downgrade() -> None:
    op.drop_column('mandant', 'angebotsnummer_zaehler')
    op.drop_index(op.f('ix_zahlung_rechnung_id'), table_name='zahlung')
    op.drop_index(op.f('ix_zahlung_mandant_id'), table_name='zahlung')
    op.drop_table('zahlung')
    op.drop_index(op.f('ix_mahnung_rechnung_id'), table_name='mahnung')
    op.drop_index(op.f('ix_mahnung_mandant_id'), table_name='mahnung')
    op.drop_table('mahnung')
    op.drop_index(op.f('ix_angebot_mandant_id'), table_name='angebot')
    op.drop_index(op.f('ix_angebot_kunde_id'), table_name='angebot')
    op.drop_table('angebot')
    op.drop_index(op.f('ix_anfrage_mandant_id'), table_name='anfrage')
    op.drop_table('anfrage')
    op.drop_index(op.f('ix_vertrag_mandant_id'), table_name='vertrag')
    op.drop_index(op.f('ix_vertrag_kunde_id'), table_name='vertrag')
    op.drop_table('vertrag')
    op.drop_index('ix_abwesenheit_ressource', table_name='abwesenheit')
    op.drop_index(op.f('ix_abwesenheit_mandant_id'), table_name='abwesenheit')
    op.drop_table('abwesenheit')
    op.drop_index(op.f('ix_artikel_mandant_id'), table_name='artikel')
    op.drop_table('artikel')
