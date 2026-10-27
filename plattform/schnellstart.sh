#!/bin/sh
# Schnellstart in einem Befehl: Plattform starten und den ersten Betrieb einrichten.
#
#   ./schnellstart.sh --name "Autohaus Beispiel" --branche kfz
#   ./schnellstart.sh --name "Müller Haustechnik" --branche shk --mitarbeiter "Anna Weber:gas,heizung" --sachmittel "Bühne 1:hebebuehne"
#   ./schnellstart.sh --demo                       # fiktive Demodaten statt eigenem Betrieb
#   ./schnellstart.sh --ohne-docker …              # Python-Umgebung statt Container (Entwicklung)
#   ./schnellstart.sh --postgres …                 # PostgreSQL per docker compose statt SQLite (mehrere Betriebe, Dauerbetrieb)
#
# Standard: ein Container (Image wird gebaut oder aus SHK_IMAGE gezogen), SQLite im Docker-Volume shk-daten,
# API-Schlüssel in .schnellstart.env (nur für den Aufrufenden lesbar). Ein zweiter Aufruf nutzt Daten und Schlüssel weiter
# und legt nichts doppelt an. Für Internet-Betrieb mit TLS, Anmeldung und Telefonie: HOSTING.md (deploy/server/einrichten.sh).
set -eu
cd "$(dirname "$0")"

NAME="" BRANCHE=shk PORT=${PORT:-8100} MODUS=docker DEMO=0 BASIS_URL="" TELEFON="" EMAIL=""
MITARBEITER="" SACHMITTEL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME=$2; shift 2 ;;
    --branche) BRANCHE=$2; shift 2 ;;
    --port) PORT=$2; shift 2 ;;
    --mitarbeiter) MITARBEITER="$MITARBEITER --mitarbeiter \"$2\""; shift 2 ;;
    --sachmittel) SACHMITTEL="$SACHMITTEL --sachmittel \"$2\""; shift 2 ;;
    --basis-url) BASIS_URL=$2; shift 2 ;;
    --telefon) TELEFON=$2; shift 2 ;;
    --email) EMAIL=$2; shift 2 ;;
    --demo) DEMO=1; shift ;;
    --ohne-docker) MODUS=python; shift ;;
    --postgres) MODUS=postgres; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "Unbekannte Option $1 (siehe --help)" >&2; exit 2 ;;
  esac
done
[ "$DEMO" = 1 ] || [ -n "$NAME" ] || { echo "Bitte --name \"Name des Betriebs\" angeben (oder --demo)." >&2; exit 2; }
[ -n "$BASIS_URL" ] || BASIS_URL="http://localhost:$PORT"

zufall() { if command -v openssl > /dev/null; then openssl rand -base64 32 | tr -d '/+=' | cut -c1-40; else python3 -c 'import secrets;print(secrets.token_urlsafe(30))'; fi; }
umask 077
if [ ! -f .schnellstart.env ]; then
  echo "CORE_API_KEY=$(zufall)" > .schnellstart.env
fi
# shellcheck disable=SC1091
. ./.schnellstart.env
export CORE_API_KEY

EINRICHTEN="python -m core einrichten --name \"$NAME\" --branche $BRANCHE --basis-url $BASIS_URL $MITARBEITER $SACHMITTEL"
[ -n "$TELEFON" ] && EINRICHTEN="$EINRICHTEN --telefon \"$TELEFON\""
[ -n "$EMAIL" ] && EINRICHTEN="$EINRICHTEN --email \"$EMAIL\""

fertig() { # fertig <URL> <Hinweis zum Stoppen>
  echo
  echo "Fertig. Büro-Oberfläche:  $1/   (Anmeldung mit dem API-Schlüssel aus .schnellstart.env)"
  echo "API-Dokumentation:        $1/docs"
  echo "API-Schlüssel:            $CORE_API_KEY"
  [ -n "${MANDANT:-}" ] && echo "Mandant:                  $MANDANT"
  echo "Kunden importieren:       $2 python -m core import-kunden kunden.csv --mandant <ID>"
  echo "Nächste Schritte zeigt die Oberfläche unter „Einrichtung“."
}

case "$MODUS" in
  python)
    if [ -z "${VIRTUAL_ENV:-}" ]; then
      [ -d .venv ] || python3 -m venv .venv
      . .venv/bin/activate
    fi
    pip install -q -r requirements.txt
    export DATABASE_URL="sqlite:///$(pwd)/daten.db" SYSTEM_DATABASE_URL="sqlite:///$(pwd)/daten.db"
    if [ "$DEMO" = 1 ]; then
      MANDANT=$(python -m core seed)
    else
      MANDANT=$(eval "$EINRICHTEN" | python3 -c 'import json,sys;print(json.load(sys.stdin)["mandant"])')
    fi
    fertig "http://localhost:$PORT" ""
    echo "Server läuft im Vordergrund (Strg+C beendet)."
    exec python -m core serve --port "$PORT"
    ;;
  docker)
    command -v docker > /dev/null || { echo "Docker fehlt (https://docs.docker.com/get-docker/); alternativ --ohne-docker." >&2; exit 1; }
    IMAGE=${SHK_IMAGE:-shk-core:schnell}
    if [ -n "${SHK_IMAGE:-}" ]; then docker pull -q "$SHK_IMAGE"; else echo "Baue Image $IMAGE …"; docker build -q -t "$IMAGE" . > /dev/null; fi
    docker volume create shk-daten > /dev/null
    docker rm -f shk-core > /dev/null 2>&1 || true
    docker run -d --name shk-core --restart unless-stopped -p "127.0.0.1:$PORT:8100" -v shk-daten:/app/daten \
      -e DATABASE_URL=sqlite:////app/daten/plattform.db -e SYSTEM_DATABASE_URL=sqlite:////app/daten/plattform.db \
      -e CORE_API_KEY="$CORE_API_KEY" -e BASIS_URL="$BASIS_URL" "$IMAGE" > /dev/null
    for i in $(seq 1 30); do
      docker exec shk-core python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8100/health')" > /dev/null 2>&1 && break
      sleep 1
    done
    if [ "$DEMO" = 1 ]; then
      MANDANT=$(docker exec shk-core python -m core seed)
    else
      MANDANT=$(docker exec shk-core sh -c "$EINRICHTEN" | python3 -c 'import json,sys;print(json.load(sys.stdin)["mandant"])')
    fi
    fertig "http://localhost:$PORT" "docker exec -i shk-core"
    echo "Stoppen: docker stop shk-core   Protokoll: docker logs -f shk-core   Daten: Volume shk-daten"
    ;;
  postgres)
    command -v docker > /dev/null || { echo "Docker fehlt." >&2; exit 1; }
    if [ ! -f .env ]; then
      sed -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(zufall)|" -e "s|^SHK_APP_PASSWORD=.*|SHK_APP_PASSWORD=$(zufall)|" \
          -e "s|^SHK_SYSTEM_PASSWORD=.*|SHK_SYSTEM_PASSWORD=$(zufall)|" -e "s|^CORE_API_KEY=.*|CORE_API_KEY=$CORE_API_KEY|" \
          -e "s|^INTEGRATIONS_KEY=.*|INTEGRATIONS_KEY=$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')|" \
          -e "s|^KEYCLOAK_ADMIN_PASSWORD=.*|KEYCLOAK_ADMIN_PASSWORD=$(zufall)|" -e "s|^KEYCLOAK_DB_PASSWORD=.*|KEYCLOAK_DB_PASSWORD=$(zufall)|" \
          -e "s|^BETRIEB_NAME=.*|BETRIEB_NAME=$NAME|" .env.example > .env
      echo ".env mit Zufallsgeheimnissen angelegt."
    fi
    docker compose up -d --build --quiet-pull postgres migrate core worker
    for i in $(seq 1 60); do curl -fsS "http://127.0.0.1:8100/health" > /dev/null 2>&1 && break; sleep 2; done
    if [ "$DEMO" = 1 ]; then
      MANDANT=$(docker compose exec -T core python -m core seed)
    else
      MANDANT=$(docker compose exec -T core sh -c "$EINRICHTEN" | python3 -c 'import json,sys;print(json.load(sys.stdin)["mandant"])')
    fi
    fertig "http://localhost:8100" "docker compose exec -T core"
    echo "Stoppen: docker compose down   Sicherung: deploy/server/backup.sh   Internet-Betrieb: HOSTING.md"
    ;;
esac
