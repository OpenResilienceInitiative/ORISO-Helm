#!/bin/bash
# Renders the pre-dev chart against the live cluster (server-side dry run). Changes nothing.
# When a required value is missing, the dry run is repeated with a placeholder for it (in the
# dry run only), so one run lists every missing value. Prints setting names and images, never
# values or manifests; quoted text in errors is masked.
source "$(dirname "$0")/lib.sh"
# Empty arrays count as unset under `set -u` on older bash; this script checks them itself.
set +u
update_chart
cd "$CHART"
out=$(mktemp); trap 'rm -f "$out" "$out.err"' EXIT
probe=()
missing=()
for attempt in $(seq 1 15); do
	if helm upgrade "$RELEASE" . -n "$NS" $(values_args) "${probe[@]}" --dry-run=server >"$out" 2>"$out.err"; then
		break
	fi
	key=$(grep -oE '[A-Za-z0-9_.-]+ (is|are) required' "$out.err" | head -1 | awk '{print $1}' || true)
	if [ -z "$key" ] || { [ ${#missing[@]} -gt 0 ] && printf '%s\n' "${missing[@]}" | grep -qxF "$key"; }; then
		echo "dry run: FAILED (not a missing value)"
		grep -vE '^coalesce.go' "$out.err" | sed -E 's/"[^"]*"/"…"/g; s/'"'"'[^'"'"']*'"'"'/'"'"'…'"'"'/g' | head -20
		exit 0
	fi
	missing+=("$key")
	probe+=(--set-string "$key=probe-placeholder")
done
if [ ${#missing[@]} -gt 0 ]; then
	echo "dry run: missing required values (${#missing[@]}):"
	printf '  - %s\n' "${missing[@]}"
	echo "(with placeholders for these, the rest of the chart renders)"
else
	echo "dry run: OK"
fi
echo "== images after an upgrade"
grep -E '^\s+image: ' "$out" | sed 's/^ *//' | sort -u || true
