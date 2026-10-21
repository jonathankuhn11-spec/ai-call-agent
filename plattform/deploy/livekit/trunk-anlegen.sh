#!/bin/sh
# Legt Inbound-Trunk und Dispatch-Regel in LiveKit an (idempotent). Aufruf auf dem Server:
#   deploy/livekit/trunk-anlegen.sh
# Liest LIVEKIT_API_KEY/LIVEKIT_API_SECRET aus ../../.env, nutzt die LiveKit-CLI `lk`
# (wird bei Bedarf nach ~/.local/bin installiert). Vorher inbound-trunk.json ausfüllen (TELEFONIE.md).
set -eu
HIER=$(cd "$(dirname "$0")" && pwd)
WURZEL=$(cd "$HIER/../.." && pwd)
[ -f "$WURZEL/.env" ] && { set -a; . "$WURZEL/.env"; set +a; }
: "${LIVEKIT_API_KEY:?LIVEKIT_API_KEY fehlt (.env)}"
: "${LIVEKIT_API_SECRET:?LIVEKIT_API_SECRET fehlt (.env)}"
export LIVEKIT_URL="${LIVEKIT_URL:-http://127.0.0.1:7880}" LIVEKIT_API_KEY LIVEKIT_API_SECRET
grep -q BITTE-EINTRAGEN "$HIER/inbound-trunk.json" && { echo "inbound-trunk.json ist noch nicht ausgefüllt." >&2; exit 1; }

if ! command -v lk > /dev/null 2>&1; then
  echo "LiveKit-CLI fehlt, installiere nach ~/.local/bin"
  mkdir -p "$HOME/.local/bin"
  curl -fsSL https://get.livekit.io/cli | BINDIR="$HOME/.local/bin" sh
  export PATH="$HOME/.local/bin:$PATH"
fi

NAME_TRUNK=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["trunk"]["name"])' "$HIER/inbound-trunk.json")
NAME_REGEL=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["dispatch_rule"]["name"])' "$HIER/dispatch-rule.json")

# Vorhandene Objekte gleichen Namens entfernen (lk gibt IDs als erste Spalte aus)
lk sip dispatch list 2>/dev/null | awk -v n="$NAME_REGEL" '$0 ~ n {print $1}' | grep '^SDR_' | while read -r id; do lk sip dispatch delete "$id"; done || true
lk sip inbound list 2>/dev/null | awk -v n="$NAME_TRUNK" '$0 ~ n {print $1}' | grep '^ST_' | while read -r id; do lk sip inbound delete "$id"; done || true

lk sip inbound create "$HIER/inbound-trunk.json"
lk sip dispatch create "$HIER/dispatch-rule.json"
echo "Trunk '$NAME_TRUNK' und Dispatch-Regel '$NAME_REGEL' angelegt. Prüfung: python -m voice.probe"
