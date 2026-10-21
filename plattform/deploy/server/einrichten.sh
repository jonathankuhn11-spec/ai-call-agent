#!/bin/sh
# Erstausrollung auf einem frisch per cloud-init eingerichteten Server (HOSTING.md, Schritt 3 und 4).
# Aufruf als Nutzer shk auf dem Server:
#   cd /opt/shk/plattform && deploy/server/einrichten.sh <version> [<git-ref>]
# Erzeugt .env mit Zufallsgeheimnissen (fragt nur Domain, E-Mail und Büro-Netz ab), prüft DNS,
# ruft deploy.sh auf und legt den ersten Mandanten an. Läuft nur, wenn noch keine .env existiert;
# ein zweiter Aufruf bricht ab, damit nichts überschrieben wird.
set -eu
VERSION=${1:?Version angeben, z. B. 0.2.0}
REF=${2:-plattform/backend-grundgeruest}
cd "$(dirname "$0")/../.."
WURZEL=$(pwd)

[ -f .env ] && { echo ".env existiert bereits; einrichten.sh ist nur für die Erstausrollung. Weiter mit deploy/server/deploy.sh $VERSION" >&2; exit 1; }
command -v docker > /dev/null || { echo "Docker fehlt; cloud-init abwarten oder HOSTING.md befolgen." >&2; exit 1; }
docker info > /dev/null 2>&1 || { echo "Keine Berechtigung für Docker; nach cloud-init einmal neu anmelden (Gruppe docker)." >&2; exit 1; }

frage() { # frage <Variable> <Text> [<Vorgabe>]
  if [ -n "${3:-}" ]; then printf '%s [%s]: ' "$2" "$3"; else printf '%s: ' "$2"; fi
  read -r antwort < /dev/tty
  eval "$1=\"\${antwort:-\${3:-}}\""
}
frage SHK_DOMAIN "Domain der Büro-Oberfläche (z. B. backoffice.beispiel.de)"
frage ACME_EMAIL "E-Mail für Let's-Encrypt-Benachrichtigungen"
frage SHK_ADMIN_NETZE "Netz mit Zugriff auf die Keycloak-Konsole (Büro-IP/32)" "$(who am i 2>/dev/null | sed -n 's/.*(\(.*\)).*/\1/p' | head -1 | sed 's#$#/32#')"
frage BETRIEB_NAME "Name des ersten Betriebs (Mandant)" "Pilotbetrieb"
AUTH_DOMAIN="auth.${SHK_DOMAIN#backoffice.}"
[ "$AUTH_DOMAIN" = "auth.$SHK_DOMAIN" ] && AUTH_DOMAIN="auth.$SHK_DOMAIN"

IP=$(curl -fsS -4 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
for h in "$SHK_DOMAIN" "$AUTH_DOMAIN"; do
  A=$(getent ahostsv4 "$h" | awk '{print $1; exit}' || true)
  if [ "$A" != "$IP" ]; then
    echo "Warnung: $h zeigt auf '${A:-nichts}', dieser Server hat $IP. Let's Encrypt wird scheitern, bis der A-Eintrag stimmt." >&2
    frage WEITER "Trotzdem fortfahren? (j/N)" "N"; [ "$WEITER" = "j" ] || exit 1
  fi
done

zufall() { openssl rand -base64 32 | tr -d '/+=' | cut -c1-40; }
INTEGRATIONS_KEY=$(docker run --rm "ghcr.io/jonathankuhn11-spec/shk-core:$VERSION" python -m core schluessel 2>/dev/null \
  || python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')

umask 077
sed -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(zufall)|" \
    -e "s|^SHK_APP_PASSWORD=.*|SHK_APP_PASSWORD=$(zufall)|" \
    -e "s|^SHK_SYSTEM_PASSWORD=.*|SHK_SYSTEM_PASSWORD=$(zufall)|" \
    -e "s|^CORE_API_KEY=.*|CORE_API_KEY=$(zufall)|" \
    -e "s|^KEYCLOAK_ADMIN_PASSWORD=.*|KEYCLOAK_ADMIN_PASSWORD=$(zufall)|" \
    -e "s|^KEYCLOAK_DB_PASSWORD=.*|KEYCLOAK_DB_PASSWORD=$(zufall)|" \
    -e "s|^INTEGRATIONS_KEY=.*|INTEGRATIONS_KEY=$INTEGRATIONS_KEY|" \
    -e "s|^LIVEKIT_API_KEY=.*|LIVEKIT_API_KEY=$(openssl rand -hex 8)|" \
    -e "s|^LIVEKIT_API_SECRET=.*|LIVEKIT_API_SECRET=$(zufall)|" \
    -e "s|^OIDC_ISSUER=.*|OIDC_ISSUER=https://$AUTH_DOMAIN/realms/shk|" \
    -e "s|^SHK_DOMAIN=.*|SHK_DOMAIN=$SHK_DOMAIN|" \
    -e "s|^ACME_EMAIL=.*|ACME_EMAIL=$ACME_EMAIL|" \
    -e "s|^SHK_VERSION=.*|SHK_VERSION=$VERSION|" \
    -e "s|^SHK_ADMIN_NETZE=.*|SHK_ADMIN_NETZE=$SHK_ADMIN_NETZE|" \
    -e "s|^BETRIEB_NAME=.*|BETRIEB_NAME=$BETRIEB_NAME|" \
    .env.example > .env
grep -q '^INTEGRATIONS_KEY=' .env || echo "INTEGRATIONS_KEY=$INTEGRATIONS_KEY" >> .env
. ./.env
# LiveKit-Schlüsselpaar in sip.yaml spiegeln (Profil voice; harmlos, wenn Telefonie noch nicht läuft)
sed -i -e "s|^api_key: .*|api_key: $LIVEKIT_API_KEY|" -e "s|^api_secret: .*|api_secret: $LIVEKIT_API_SECRET|" deploy/livekit/sip.yaml
echo ".env mit Zufallsgeheimnissen erzeugt (Rechte 600). Sicher verwahren: $WURZEL/.env"

git -C "$WURZEL/.." fetch --quiet origin && git -C "$WURZEL/.." checkout --quiet "$REF" 2>/dev/null || true
deploy/server/deploy.sh "$VERSION"

MANDANT=$(curl -fsS -H "X-API-Key: $CORE_API_KEY" -H 'Content-Type: application/json' \
  -d "{\"name\":\"$BETRIEB_NAME\"}" http://127.0.0.1:8100/mandanten | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
sed -i "s|^CORE_MANDANT=.*|CORE_MANDANT=$MANDANT|" .env
cat <<ENDE

Erstausrollung abgeschlossen.
  Oberfläche:      https://$SHK_DOMAIN
  Keycloak:        https://$AUTH_DOMAIN/admin  (nur aus $SHK_ADMIN_NETZE; Bootstrap-Admin: admin / siehe KEYCLOAK_ADMIN_PASSWORD in .env)
  Mandanten-ID:    $MANDANT  (in .env als CORE_MANDANT eingetragen; Benutzer in Keycloak brauchen dieses Attribut 'mandant')
Nächste Schritte: HOSTING.md Schritt 6 (Benutzer), TELEFONIE.md (SIP-Trunk).
ENDE
