#!/bin/bash
# Renders the pre-dev chart against the live cluster (server-side dry run). Prints only
# Helm's errors and the images the upgrade would roll out, never the manifests.
source "$(dirname "$0")/lib.sh"
update_chart
cd "$CHART"
out=$(mktemp); trap 'rm -f "$out"' EXIT
if helm upgrade "$RELEASE" . -n "$NS" $(values_args) --dry-run=server >"$out" 2>"$out.err"; then
	echo "dry run: OK"
	echo "== images after an upgrade"
	grep -E '^\s+image: ' "$out" | sed 's/^ *//' | sort -u
else
	echo "dry run: FAILED"
	# Helm names the missing or invalid setting; quoted values are masked so nothing secret leaks.
	grep -vE '^coalesce.go' "$out.err" | sed -E 's/"[^"]*"/"…"/g; s/'"'"'[^'"'"']*'"'"'/'"'"'…'"'"'/g' | head -40
fi
rm -f "$out.err"
