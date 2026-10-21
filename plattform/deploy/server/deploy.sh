#!/bin/sh
# Ausrollen einer Version auf dem Server (aufgerufen vom Workflow plattform-deploy oder von Hand):
#   deploy/server/deploy.sh 0.2.0
# Ablauf: Image ziehen, Sicherung, Migrationen, Dienste neu starten, Gesundheitsprüfung;
# schlägt die Prüfung fehl, wird auf die vorige Version zurückgestellt (Migrationen sind vorwärtskompatibel zu halten).
set -eu
VERSION=${1:?Version angeben}
cd "$(dirname "$0")/../.."
COMPOSE="docker compose -f docker-compose.yml -f deploy/server/docker-compose.prod.yml --profile auth ${SHK_PROFILE_ZUSATZ:-}"
VORHER=$(cat .shk-version 2>/dev/null || echo "")
export SHK_VERSION="$VERSION"
$COMPOSE pull --quiet migrate core worker
[ -n "$VORHER" ] && deploy/server/backup.sh
$COMPOSE run --rm migrate
$COMPOSE up -d --remove-orphans
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8100/health > /dev/null 2>&1; then
    echo "$VERSION" > .shk-version
    echo "Version $VERSION läuft."
    exit 0
  fi
  sleep 2
done
echo "Gesundheitsprüfung fehlgeschlagen." >&2
$COMPOSE logs --tail 50 core >&2
if [ -n "$VORHER" ]; then
  echo "Zurück auf $VORHER." >&2
  SHK_VERSION="$VORHER" $COMPOSE up -d core worker
fi
exit 1
