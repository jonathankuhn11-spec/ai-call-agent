#!/usr/bin/env sh
# Prüft XRechnung-Dateien mit dem KoSIT-Validator (Java 11+), Konfiguration XRechnung 3.0.2.
# Vorher einmalig: drittanbieter/spiegeln.sh (lädt und entpackt Validator und Konfiguration).
# Aufruf: drittanbieter/xrechnung/pruefen.sh rechnung.xml [weitere.xml …]   Berichte landen in drittanbieter/xrechnung/berichte/
set -eu
HIER=$(cd "$(dirname "$0")" && pwd)
JAR="$HIER/validationtool-1.5.0-standalone.jar"
[ -f "$JAR" ] || { echo "Validator fehlt: erst drittanbieter/spiegeln.sh ausführen" >&2; exit 2; }
mkdir -p "$HIER/berichte"
exec java -jar "$JAR" -s "$HIER/konfiguration/scenarios.xml" -r "$HIER/konfiguration" -o "$HIER/berichte" -h "$@"
