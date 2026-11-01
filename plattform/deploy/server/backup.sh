#!/bin/sh
# Nächtliche Sicherung: PostgreSQL (alle Datenbanken inkl. Keycloak) als pg_dump im Custom-Format,
# Aufbewahrung lokal 14 Tage; optional verschlüsselt außer Haus mit restic (z. B. Hetzner Storage Box).
# Aufruf per cron (cloud-init.yaml) als Nutzer shk: /opt/shk/plattform/deploy/server/backup.sh
set -eu
cd "$(dirname "$0")/../.."
# nur die Sicherungsvariablen aus .env übernehmen (die Datei ist kein Shell-Skript)
[ -f .env ] && eval "$(grep -E '^(RESTIC_REPOSITORY|RESTIC_PASSWORD_FILE|SHK_BACKUP_[A-Z]+)=' .env | sed -e 's/[[:space:]]*#.*$//' -e 's/^/export /')"
ZIEL=${SHK_BACKUP_VERZEICHNIS:-/var/backups/shk}
TAGE=${SHK_BACKUP_TAGE:-14}
STEMPEL=$(date -u +%Y%m%dT%H%M%SZ)
COMPOSE="docker compose -f docker-compose.yml -f deploy/server/docker-compose.prod.yml"
mkdir -p "$ZIEL"
for db in shk keycloak; do
  if $COMPOSE exec -T postgres psql -U shk -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = '$db'" | grep -q 1; then
    $COMPOSE exec -T postgres pg_dump -U shk -d "$db" -Fc --no-owner > "$ZIEL/$db-$STEMPEL.dump.tmp"
    mv "$ZIEL/$db-$STEMPEL.dump.tmp" "$ZIEL/$db-$STEMPEL.dump"
  fi
done
$COMPOSE exec -T postgres pg_dumpall -U shk --globals-only --no-role-passwords > "$ZIEL/rollen-$STEMPEL.sql"
find "$ZIEL" -type f -mtime +"$TAGE" -delete
# Außer-Haus-Kopie, wenn RESTIC_REPOSITORY und RESTIC_PASSWORD_FILE gesetzt sind (Ende-zu-Ende verschlüsselt)
if [ -n "${RESTIC_REPOSITORY:-}" ]; then
  restic backup --quiet --tag shk "$ZIEL"
  restic forget --quiet --tag shk --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune
fi
echo "Sicherung $STEMPEL abgeschlossen: $(ls "$ZIEL" | grep -c "$STEMPEL") Dateien"
