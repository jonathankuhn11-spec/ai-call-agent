"""Grundschema: kanonisches Modell, Event-Log, Outbox

Revision: 0001 (Vorgänger: )
"""
from alembic import op
import sqlalchemy as sa

import core.db

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('mandant',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('gewerk', sa.String(length=50), nullable=False),
    sa.Column('policies', sa.JSON(), nullable=False),
    sa.Column('puffer_minuten', sa.Integer(), nullable=False),
    sa.Column('raster_minuten', sa.Integer(), nullable=False),
    sa.Column('erstellt', core.db.UtcZeit(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('ereignis',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('typ', sa.String(length=80), nullable=False),
    sa.Column('entitaet', sa.String(length=40), nullable=False),
    sa.Column('entitaet_id', sa.String(length=32), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('akteur', sa.String(length=10), nullable=False),
    sa.Column('akteur_name', sa.String(length=100), nullable=True),
    sa.Column('zeit', core.db.UtcZeit(timezone=True), nullable=False),
    sa.Column('mandant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ereignis_entitaet_id'), 'ereignis', ['entitaet_id'], unique=False)
    op.create_index(op.f('ix_ereignis_mandant_id'), 'ereignis', ['mandant_id'], unique=False)
    op.create_index(op.f('ix_ereignis_typ'), 'ereignis', ['typ'], unique=False)
    op.create_table('external_ref',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('entitaet', sa.String(length=40), nullable=False),
    sa.Column('entitaet_id', sa.String(length=32), nullable=False),
    sa.Column('zielsystem', sa.String(length=50), nullable=False),
    sa.Column('externe_id', sa.String(length=120), nullable=False),
    sa.Column('letzter_sync', core.db.UtcZeit(timezone=True), nullable=False),
    sa.Column('mandant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('mandant_id', 'entitaet', 'entitaet_id', 'zielsystem')
    )
    op.create_index(op.f('ix_external_ref_mandant_id'), 'external_ref', ['mandant_id'], unique=False)
    op.create_table('kunde',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('telefon', sa.String(length=32), nullable=True),
    sa.Column('email', sa.String(length=200), nullable=True),
    sa.Column('strasse', sa.String(length=200), nullable=True),
    sa.Column('plz', sa.String(length=10), nullable=True),
    sa.Column('ort', sa.String(length=100), nullable=True),
    sa.Column('einwilligungen', sa.JSON(), nullable=False),
    sa.Column('vertragsstatus', sa.String(length=30), nullable=False),
    sa.Column('notiz', sa.Text(), nullable=True),
    sa.Column('erstellt', core.db.UtcZeit(timezone=True), nullable=False),
    sa.Column('mandant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_kunde_mandant_id'), 'kunde', ['mandant_id'], unique=False)
    op.create_index(op.f('ix_kunde_telefon'), 'kunde', ['telefon'], unique=False)
    op.create_table('outbox',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('ziel', sa.String(length=50), nullable=False),
    sa.Column('aktion', sa.String(length=50), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('idempotenz_schluessel', sa.String(length=120), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('versuche', sa.Integer(), nullable=False),
    sa.Column('naechster_versuch', core.db.UtcZeit(timezone=True), nullable=False),
    sa.Column('letzter_fehler', sa.Text(), nullable=True),
    sa.Column('mandant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('mandant_id', 'idempotenz_schluessel')
    )
    op.create_index(op.f('ix_outbox_mandant_id'), 'outbox', ['mandant_id'], unique=False)
    op.create_table('ressource',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('qualifikationen', sa.JSON(), nullable=False),
    sa.Column('arbeitstage', sa.JSON(), nullable=False),
    sa.Column('arbeitsbeginn', sa.Time(), nullable=False),
    sa.Column('arbeitsende', sa.Time(), nullable=False),
    sa.Column('aktiv', sa.Boolean(), nullable=False),
    sa.Column('mandant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ressource_mandant_id'), 'ressource', ['mandant_id'], unique=False)
    op.create_table('objekt',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('kunde_id', sa.String(length=32), nullable=False),
    sa.Column('bezeichnung', sa.String(length=200), nullable=False),
    sa.Column('strasse', sa.String(length=200), nullable=True),
    sa.Column('plz', sa.String(length=10), nullable=True),
    sa.Column('ort', sa.String(length=100), nullable=True),
    sa.Column('gebaeudetyp', sa.String(length=50), nullable=True),
    sa.Column('mandant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['kunde_id'], ['kunde.id'], ),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_objekt_kunde_id'), 'objekt', ['kunde_id'], unique=False)
    op.create_index(op.f('ix_objekt_mandant_id'), 'objekt', ['mandant_id'], unique=False)
    op.create_table('anlage',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('objekt_id', sa.String(length=32), nullable=False),
    sa.Column('anlagentyp', sa.String(length=50), nullable=False),
    sa.Column('hersteller', sa.String(length=100), nullable=True),
    sa.Column('modell', sa.String(length=100), nullable=True),
    sa.Column('baujahr', sa.Integer(), nullable=True),
    sa.Column('wartungsintervall_monate', sa.Integer(), nullable=True),
    sa.Column('letzte_wartung', sa.Date(), nullable=True),
    sa.Column('mandant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandant.id'], ),
    sa.ForeignKeyConstraint(['objekt_id'], ['objekt.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_anlage_mandant_id'), 'anlage', ['mandant_id'], unique=False)
    op.create_index(op.f('ix_anlage_objekt_id'), 'anlage', ['objekt_id'], unique=False)
    op.create_table('servicefall',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('kunde_id', sa.String(length=32), nullable=False),
    sa.Column('anlage_id', sa.String(length=32), nullable=True),
    sa.Column('anliegen', sa.Text(), nullable=False),
    sa.Column('symptom', sa.Text(), nullable=True),
    sa.Column('dringlichkeit', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('quelle', sa.String(length=20), nullable=False),
    sa.Column('benoetigte_qualifikation', sa.String(length=50), nullable=True),
    sa.Column('dauer_minuten', sa.Integer(), nullable=False),
    sa.Column('erstellt', core.db.UtcZeit(timezone=True), nullable=False),
    sa.Column('geaendert', core.db.UtcZeit(timezone=True), nullable=False),
    sa.Column('mandant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['anlage_id'], ['anlage.id'], ),
    sa.ForeignKeyConstraint(['kunde_id'], ['kunde.id'], ),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_servicefall_kunde_id'), 'servicefall', ['kunde_id'], unique=False)
    op.create_index(op.f('ix_servicefall_mandant_id'), 'servicefall', ['mandant_id'], unique=False)
    op.create_table('freigabe',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('aktion', sa.String(length=50), nullable=False),
    sa.Column('risikoklasse', sa.String(length=1), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('servicefall_id', sa.String(length=32), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('begruendung', sa.Text(), nullable=True),
    sa.Column('erstellt', core.db.UtcZeit(timezone=True), nullable=False),
    sa.Column('entschieden_von', sa.String(length=100), nullable=True),
    sa.Column('entschieden_am', core.db.UtcZeit(timezone=True), nullable=True),
    sa.Column('mandant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandant.id'], ),
    sa.ForeignKeyConstraint(['servicefall_id'], ['servicefall.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_freigabe_mandant_id'), 'freigabe', ['mandant_id'], unique=False)
    op.create_table('kommunikation',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('kunde_id', sa.String(length=32), nullable=True),
    sa.Column('servicefall_id', sa.String(length=32), nullable=True),
    sa.Column('kanal', sa.String(length=20), nullable=False),
    sa.Column('richtung', sa.String(length=10), nullable=False),
    sa.Column('transkript', sa.Text(), nullable=True),
    sa.Column('zusammenfassung', sa.Text(), nullable=True),
    sa.Column('extraktion', sa.JSON(), nullable=False),
    sa.Column('zeit', core.db.UtcZeit(timezone=True), nullable=False),
    sa.Column('mandant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['kunde_id'], ['kunde.id'], ),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandant.id'], ),
    sa.ForeignKeyConstraint(['servicefall_id'], ['servicefall.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_kommunikation_kunde_id'), 'kommunikation', ['kunde_id'], unique=False)
    op.create_index(op.f('ix_kommunikation_mandant_id'), 'kommunikation', ['mandant_id'], unique=False)
    op.create_table('termin',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('servicefall_id', sa.String(length=32), nullable=False),
    sa.Column('ressource_id', sa.String(length=32), nullable=False),
    sa.Column('beginn', core.db.UtcZeit(timezone=True), nullable=False),
    sa.Column('ende', core.db.UtcZeit(timezone=True), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('mandant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['mandant_id'], ['mandant.id'], ),
    sa.ForeignKeyConstraint(['ressource_id'], ['ressource.id'], ),
    sa.ForeignKeyConstraint(['servicefall_id'], ['servicefall.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_termin_mandant_id'), 'termin', ['mandant_id'], unique=False)
    op.create_index('ix_termin_ressource_zeit', 'termin', ['ressource_id', 'beginn'], unique=False)
    op.create_index(op.f('ix_termin_servicefall_id'), 'termin', ['servicefall_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_termin_servicefall_id'), table_name='termin')
    op.drop_index('ix_termin_ressource_zeit', table_name='termin')
    op.drop_index(op.f('ix_termin_mandant_id'), table_name='termin')
    op.drop_table('termin')
    op.drop_index(op.f('ix_kommunikation_mandant_id'), table_name='kommunikation')
    op.drop_index(op.f('ix_kommunikation_kunde_id'), table_name='kommunikation')
    op.drop_table('kommunikation')
    op.drop_index(op.f('ix_freigabe_mandant_id'), table_name='freigabe')
    op.drop_table('freigabe')
    op.drop_index(op.f('ix_servicefall_mandant_id'), table_name='servicefall')
    op.drop_index(op.f('ix_servicefall_kunde_id'), table_name='servicefall')
    op.drop_table('servicefall')
    op.drop_index(op.f('ix_anlage_objekt_id'), table_name='anlage')
    op.drop_index(op.f('ix_anlage_mandant_id'), table_name='anlage')
    op.drop_table('anlage')
    op.drop_index(op.f('ix_objekt_mandant_id'), table_name='objekt')
    op.drop_index(op.f('ix_objekt_kunde_id'), table_name='objekt')
    op.drop_table('objekt')
    op.drop_index(op.f('ix_ressource_mandant_id'), table_name='ressource')
    op.drop_table('ressource')
    op.drop_index(op.f('ix_outbox_mandant_id'), table_name='outbox')
    op.drop_table('outbox')
    op.drop_index(op.f('ix_kunde_telefon'), table_name='kunde')
    op.drop_index(op.f('ix_kunde_mandant_id'), table_name='kunde')
    op.drop_table('kunde')
    op.drop_index(op.f('ix_external_ref_mandant_id'), table_name='external_ref')
    op.drop_table('external_ref')
    op.drop_index(op.f('ix_ereignis_typ'), table_name='ereignis')
    op.drop_index(op.f('ix_ereignis_mandant_id'), table_name='ereignis')
    op.drop_index(op.f('ix_ereignis_entitaet_id'), table_name='ereignis')
    op.drop_table('ereignis')
    op.drop_table('mandant')
