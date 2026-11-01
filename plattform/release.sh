#!/bin/sh
# Release-Paket bauen: Quellarchiv, Container-Image (optional als Archiv), Prüfsummen und Begleitdateien in dist/<version>/.
#
#   ./release.sh                 # Quellarchiv + Image bauen, Image als tar.gz exportieren
#   ./release.sh --ohne-image    # nur Quellarchiv (für Installationen, die das Image aus GHCR ziehen)
#   ./release.sh --push REG      # Image zusätzlich nach REG/shk-core:<version> und :latest schieben
#
# Das Paket enthält alles, was ein Kunde für den Betrieb braucht: Quelltext mit Lizenztexten (drittanbieter/), Compose-Dateien,
# HOSTING.md, CHANGELOG.md, VERSION. Lizenzschlüssel werden getrennt mit `python -m core lizenz erzeugen` ausgestellt.
set -eu
cd "$(dirname "$0")"
VERSION=$(cat VERSION)
MIT_IMAGE=1 PUSH=""
while [ $# -gt 0 ]; do
  case "$1" in
    --ohne-image) MIT_IMAGE=0; shift ;;
    --push) PUSH=$2; shift 2 ;;
    *) echo "Unbekannte Option $1" >&2; exit 2 ;;
  esac
done
ZIEL=dist/$VERSION
rm -rf "$ZIEL" && mkdir -p "$ZIEL"
NAME=shk-plattform-$VERSION

# Quellarchiv aus dem Git-Stand (ohne .env, Datenbanken, Wheelhouse); Prefix = Versionsordner
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git archive --format=tar.gz --prefix="$NAME/" -o "$ZIEL/$NAME.tar.gz" HEAD .
else
  tar --exclude='*.db' --exclude='.env*' --exclude='drittanbieter/wheelhouse' --exclude='drittanbieter/images' --exclude='dist' \
      --transform "s,^\.,$NAME," -czf "$ZIEL/$NAME.tar.gz" .
fi
cp CHANGELOG.md HOSTING.md VERSION "$ZIEL/"

if [ "$MIT_IMAGE" = "1" ]; then
  docker build -t "shk-core:$VERSION" -t shk-core:latest .
  docker save "shk-core:$VERSION" | gzip > "$ZIEL/shk-core-$VERSION.image.tar.gz"
  if [ -n "$PUSH" ]; then
    docker tag "shk-core:$VERSION" "$PUSH/shk-core:$VERSION" && docker tag "shk-core:$VERSION" "$PUSH/shk-core:latest"
    docker push "$PUSH/shk-core:$VERSION" && docker push "$PUSH/shk-core:latest"
  fi
fi

(cd "$ZIEL" && sha256sum -- * > SHA256SUMS)
echo "Release $VERSION in $ZIEL:"
ls -l "$ZIEL"
