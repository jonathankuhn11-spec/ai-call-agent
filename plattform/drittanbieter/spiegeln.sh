#!/usr/bin/env sh
# Spiegelt alle Fremdkomponenten der Plattform in dieses Verzeichnis, damit Bau und Betrieb ohne externe Quellen möglich sind:
#   wheelhouse/   Python-Pakete (requirements.txt, mit --voice auch requirements-voice.txt) für Python 3.12, manylinux x86_64
#   xrechnung/    KoSIT-Validator und XRechnung-Konfiguration (Prüfsummen in SHA256SUMS)
#   ontologie/    Brick-Ontologie (bereits im Repository, wird nur geprüft)
#   images/       Container-Images aus IMAGES.txt als tar (docker save), nur mit --images
# Aufruf: drittanbieter/spiegeln.sh [--voice] [--images] [--pruefen]
set -eu
HIER=$(cd "$(dirname "$0")" && pwd)
WURZEL=$(dirname "$HIER")
VOICE=0; IMAGES=0; NUR_PRUEFEN=0
for a in "$@"; do case "$a" in --voice) VOICE=1;; --images) IMAGES=1;; --pruefen) NUR_PRUEFEN=1;; *) echo "unbekannte Option $a" >&2; exit 2;; esac; done

if [ "$NUR_PRUEFEN" = 0 ]; then
  echo "== Python-Pakete nach wheelhouse/"
  python3 -m pip download -q -r "$WURZEL/requirements.txt" -d "$HIER/wheelhouse" \
    --python-version 3.12 --only-binary=:all: --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux_2_28_x86_64 --platform any
  if [ "$VOICE" = 1 ]; then
    python3 -m pip download -q -r "$WURZEL/requirements-voice.txt" -d "$HIER/wheelhouse" \
      --python-version 3.12 --only-binary=:all: --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux_2_28_x86_64 --platform any
  fi
  echo "== Artefakte aus QUELLEN.txt"
  grep -v '^#' "$HIER/QUELLEN.txt" | while IFS="$(printf '\t')" read -r ziel version lizenz url; do
    [ -n "$ziel" ] || continue
    if [ -s "$HIER/$ziel" ]; then echo "   vorhanden: $ziel"; else echo "   lade: $ziel ($version, $lizenz)"; curl -fsSL --retry 3 -o "$HIER/$ziel" "$url"; fi
  done
  if [ "$IMAGES" = 1 ]; then
    echo "== Container-Images nach images/"
    grep -v '^#' "$HIER/IMAGES.txt" | while read -r img; do
      [ -n "$img" ] || continue
      datei="$HIER/images/$(echo "$img" | tr '/:' '__').tar"
      if [ -s "$datei" ]; then echo "   vorhanden: $img"; else echo "   ziehe: $img"; docker pull -q "$img" >/dev/null && docker save -o "$datei" "$img"; fi
    done
  fi
fi

echo "== Prüfsummen"
(cd "$HIER" && sha256sum -c SHA256SUMS)
if [ -d "$HIER/xrechnung" ] && [ -s "$HIER/xrechnung/validator-1.5.0-distribution.zip" ] && [ ! -f "$HIER/xrechnung/validationtool-1.5.0-standalone.jar" ]; then
  echo "== Validator entpacken"
  (cd "$HIER/xrechnung" && unzip -oq validator-1.5.0-distribution.zip validationtool-1.5.0-standalone.jar LICENSE NOTICE \
     && mkdir -p konfiguration && unzip -oq validator-configuration-xrechnung_3.0.2_2025-07-10.zip -d konfiguration)
fi
echo "fertig: $(ls "$HIER/wheelhouse" 2>/dev/null | wc -l) Wheels, $(ls "$HIER/images" 2>/dev/null | grep -c tar || true) Image-Archive"
