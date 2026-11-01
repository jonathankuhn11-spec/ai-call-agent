"""Row-Level Security je Mandant (nur PostgreSQL)

Jede Fachtabelle ist nur für den Mandanten sichtbar und beschreibbar, der in der Transaktion als
`app.mandant` gesetzt ist (core.db.mandant_binden). Ohne gesetzten Mandanten liefert eine Abfrage
keine Zeilen, statt fremde Daten preiszugeben. Das schützt gegen vergessene Filter im
Anwendungscode (Defense in Depth); die Filter auf mandant_id im Code bleiben bestehen.

Rollen (angelegt durch deploy/postgres/rollen.sql, nicht durch die Migration):
  shk_app     Core-Service, unterliegt RLS
  shk_system  Outbox-Worker, Seed, Mandantenverwaltung; BYPASSRLS
Superuser und (ohne FORCE) Tabelleneigentümer umgehen RLS immer; deshalb FORCE und eigene Rollen.

Revision: 0002 (Vorgänger: 0001)
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

FACHTABELLEN = ("kunde", "objekt", "anlage", "ressource", "servicefall", "termin", "kommunikation",
                "freigabe", "ereignis", "outbox", "external_ref")
AKTUELL = "current_setting('app.mandant', true)"
ROLLEN = ("shk_app", "shk_system")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for tabelle in FACHTABELLEN:
        op.execute(f"ALTER TABLE {tabelle} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tabelle} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY mandantentrennung ON {tabelle} "
                   f"USING (mandant_id = {AKTUELL}) WITH CHECK (mandant_id = {AKTUELL})")
    op.execute("ALTER TABLE mandant ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mandant FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY mandantentrennung ON mandant USING (id = {AKTUELL}) WITH CHECK (id = {AKTUELL})")
    # Rechte nur vergeben, wenn die Rollen existieren (lokal mit SQLite oder ohne Rollenkonzept: kein Fehler)
    for rolle in ROLLEN:
        op.execute(f"""
            DO $$ BEGIN
              IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{rolle}') THEN
                GRANT USAGE ON SCHEMA public TO {rolle};
                GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {rolle};
                GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {rolle};
                ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {rolle};
                ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {rolle};
                REVOKE ALL ON alembic_version FROM {rolle};
              END IF;
            END $$""")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for tabelle in (*FACHTABELLEN, "mandant"):
        op.execute(f"DROP POLICY IF EXISTS mandantentrennung ON {tabelle}")
        op.execute(f"ALTER TABLE {tabelle} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tabelle} DISABLE ROW LEVEL SECURITY")
