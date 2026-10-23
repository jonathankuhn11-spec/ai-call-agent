#!/bin/sh
# Spaltet das Verzeichnis plattform/ mit seiner Geschichte in ein eigenes Repository ab.
# Voraussetzung: ein leeres Repository auf GitHub (ohne README), z. B. jonathankuhn11-spec/shk-backoffice-plattform.
# Aufruf aus dem Monorepo:
#   plattform/deploy/repo-abspalten.sh git@github.com:jonathankuhn11-spec/shk-backoffice-plattform.git [<quell-branch>]
# Ergebnis: Branch main im Zielrepository mit dem Inhalt von plattform/ als Wurzel, Workflows nach
# .github/workflows/ verschoben und auf das neue Layout angepasst. Das Monorepo bleibt unverändert.
set -eu
ZIEL=${1:?Ziel-Repository angeben (SSH- oder HTTPS-URL)}
QUELLE=${2:-plattform/backend-grundgeruest}
WURZEL=$(git rev-parse --show-toplevel)
cd "$WURZEL"
git rev-parse --verify --quiet "$QUELLE" > /dev/null || { echo "Branch $QUELLE nicht vorhanden." >&2; exit 1; }

echo "1/4 Geschichte von plattform/ herausschneiden (git subtree split)"
SPLIT=$(git subtree split --prefix=plattform "$QUELLE")

ARBEIT=$(mktemp -d)
echo "2/4 Arbeitskopie in $ARBEIT"
git clone --quiet --no-local "$WURZEL" "$ARBEIT/kopie"
cd "$ARBEIT/kopie"
git fetch --quiet "$WURZEL" "$SPLIT:main"
git checkout --quiet main

echo "3/4 Workflows und Pfade anpassen"
mkdir -p .github/workflows
for w in plattform.yml plattform-deploy.yml; do
  Q="$WURZEL/.github/workflows/$w"; [ -f "$Q" ] || continue
  sed -e '/^    paths: \["plattform\/\*\*"/d' \
      -e '/^    defaults:$/,/working-directory: plattform/d' -e 's#docker build \(.*\) plattform$#docker build \1 .#' \
      -e 's#plattform/deploy/server/deploy.sh#deploy/server/deploy.sh#' \
      "$Q" > ".github/workflows/$w"
done
# Verweise auf das Monorepo in Skripten und Dokumentation
ZIEL_NAME=$(printf '%s' "${ZIEL%.git}" | sed -E 's#.*[:/]([^/]+)/([^/]+)$#\1/\2#')
grep -rl 'jonathankuhn11-spec/ai-call-agent' --include='*.sh' --include='*.yaml' --include='*.yml' --include='*.md' . \
  | xargs -r sed -i "s#jonathankuhn11-spec/ai-call-agent#$ZIEL_NAME#g"
sed -i 's#/opt/shk/plattform#/opt/shk#g; s#plattform/deploy/#deploy/#g' deploy/server/cloud-init.yaml deploy/server/*.sh HOSTING.md TELEFONIE.md 2>/dev/null || true
[ -f deploy/server/einrichten.sh ] && sed -i 's#^REF=\${2:-plattform/backend-grundgeruest}#REF=${2:-main}#' deploy/server/einrichten.sh
git add -A
git -c user.name="repo-abspalten" -c user.email="noreply@localhost" commit --quiet -m "Plattform als eigenständiges Repository (Workflows und Pfade angepasst)"

echo "4/4 Nach $ZIEL übertragen"
git remote add ziel "$ZIEL"
git push -u ziel main
echo "Fertig. Im Zielrepository: Settings → Actions → Workflow 'Plattform' läuft bei jedem Push; Environment 'produktion' nach HOSTING.md anlegen."
echo "Im Monorepo kann plattform/ danach entfernt werden: git rm -r plattform && git commit -m 'Plattform in eigenes Repository verschoben'"
rm -rf "$ARBEIT"
