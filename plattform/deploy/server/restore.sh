#!/bin/sh
# Wiederherstellung einer Sicherung in die laufende PostgreSQL-Instanz. Überschreibt die Datenbank!
#   deploy/server/restore.sh /var/backups/shk/shk-20261021T020000Z.dump [shk|keycloak]
# Mindestens quartalsweise als Wiederherstellungstest auf einem Testserver ausführen (TOM, Art. 32 DSGVO).
set -eu
DATEI=${1:?Pfad zur .dump-Datei angeben}
DB=${2:-shk}
cd "$(dirname "$0")/../.."
COMPOSE="docker compose -f docker-compose.yml -f deploy/server/docker-compose.prod.yml"
printf 'Datenbank %s aus %s wiederherstellen? Laufende Daten gehen verloren. [ja/NEIN] ' "$DB" "$DATEI"
read -r antwort; [ "$antwort" = "ja" ] || { echo "Abgebrochen."; exit 1; }
$COMPOSE stop core worker keycloak voice-agent 2>/dev/null || true
$COMPOSE exec -T postgres pg_restore -U shk -d "$DB" --clean --if-exists --no-owner < "$DATEI"
if [ "$DB" = "shk" ]; then  # Rechte und RLS kommen aus den Migrationen
  $COMPOSE run --rm migrate
fi
$COMPOSE up -d
echo "Wiederhergestellt. Prüfen: curl -fsS https://\$SHK_DOMAIN/health"
