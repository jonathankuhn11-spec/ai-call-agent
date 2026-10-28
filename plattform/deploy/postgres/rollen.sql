-- Datenbankrollen der Plattform. Einmalig als Eigentümer bzw. Admin ausführen:
--   psql "$MIGRATION_DATABASE_URL" -v app_passwort=… -v system_passwort=… -f rollen.sql
-- Danach `python -m core migrate`; die Migration vergibt die Tabellenrechte.
--   shk_app     Core-Service (API, Oberfläche, Voice-Tools), unterliegt Row-Level Security
--   shk_system  Outbox-Worker, Seed, Mandantenverwaltung, umgeht RLS (BYPASSRLS)
SELECT format('CREATE ROLE shk_app LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS', :'app_passwort')
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'shk_app') \gexec
SELECT format('CREATE ROLE shk_system LOGIN PASSWORD %L NOSUPERUSER BYPASSRLS', :'system_passwort')
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'shk_system') \gexec
ALTER ROLE shk_app SET statement_timeout = '15s';
SELECT format('GRANT CONNECT ON DATABASE %I TO shk_app, shk_system', current_database()) \gexec
