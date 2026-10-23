#!/bin/sh
# Eigene Datenbank und Rolle für Keycloak (nur wenn KEYCLOAK_DB_PASSWORD gesetzt ist, also im Serverbetrieb).
set -eu
[ -n "${KEYCLOAK_DB_PASSWORD:-}" ] || exit 0
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v pw="$KEYCLOAK_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE keycloak LOGIN PASSWORD %L', :'pw') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'keycloak') \gexec
SELECT 'CREATE DATABASE keycloak OWNER keycloak' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'keycloak') \gexec
SQL
