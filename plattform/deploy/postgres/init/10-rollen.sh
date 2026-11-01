#!/bin/sh
# Läuft einmalig beim ersten Start des PostgreSQL-Containers (leeres Datenvolume).
set -eu
: "${SHK_APP_PASSWORD:?SHK_APP_PASSWORD fehlt}" "${SHK_SYSTEM_PASSWORD:?SHK_SYSTEM_PASSWORD fehlt}"
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v app_passwort="$SHK_APP_PASSWORD" -v system_passwort="$SHK_SYSTEM_PASSWORD" -f /shk/rollen.sql
