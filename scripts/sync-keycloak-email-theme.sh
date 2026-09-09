#!/usr/bin/env bash
# Copies the generated Keycloak e-mail theme from ORISO-Frontend into this chart.
#
# The theme is generated from the ORISO e-mail design system
# (ORISO-Frontend `src/emails/`, `npm run emails:keycloak`) and reviewed there.
# ORISO-Keycloak carries the same theme in its image; the two have to move
# together or one silently wins at deploy time.
#
# theme.properties is deliberately NOT copied: this chart mounts only
# email/{html,messages,text} as ConfigMaps, and every template lookup carries
# its own default, so the theme renders correctly without it.
#
#   scripts/sync-keycloak-email-theme.sh [path-to-ORISO-Frontend]
set -euo pipefail

frontend="${1:-../ORISO-Frontend}"
src="$frontend/src/emails/dist/keycloak/email"

if [[ ! -d "$src" ]]; then
  echo "no generated theme at $src — run 'npm run emails:keycloak' in $frontend" >&2
  exit 1
fi

root="$(cd "$(dirname "$0")/.." && pwd)"
target="$root/charts/keycloak/keycloak-resources/custom-theme/email"

for part in html text messages; do
  mkdir -p "$target/$part"
  cp -R "$src/$part/." "$target/$part/"
done

echo "synced into charts/keycloak/keycloak-resources/custom-theme/email"
echo "run the matching script in ORISO-Keycloak too."
