#!/bin/sh
# Bereitet den Kartenausschnitt für OSRM vor (einmalig, danach bei Bedarf monatlich).
# Lädt den Regionsauszug von Geofabrik (OpenStreetMap, ODbL) und erzeugt die Routing-Daten im Volume osrm-daten.
# Nutzung: deploy/osrm/vorbereiten.sh [Geofabrik-Pfad], Standard europe/germany/nordrhein-westfalen (ca. 500 MB, 20–40 min).
set -eu
REGION="${1:-europe/germany/nordrhein-westfalen}"
IMAGE="ghcr.io/project-osrm/osrm-backend:v5.27.1"
VOLUME="$(basename "$(pwd)")_osrm-daten"
docker volume create "$VOLUME" >/dev/null
echo "Lade https://download.geofabrik.de/${REGION}-latest.osm.pbf"
docker run --rm -v "$VOLUME:/data" curlimages/curl:8.11.1 -fL -o /data/karte.osm.pbf "https://download.geofabrik.de/${REGION}-latest.osm.pbf"
docker run --rm -t -v "$VOLUME:/data" "$IMAGE" osrm-extract -p /opt/car.lua /data/karte.osm.pbf
docker run --rm -t -v "$VOLUME:/data" "$IMAGE" osrm-partition /data/karte.osrm
docker run --rm -t -v "$VOLUME:/data" "$IMAGE" osrm-customize /data/karte.osrm
echo "Fertig. Start: docker compose --profile routing up -d osrm; in .env ROUTING_URL=http://osrm:5000 setzen."
