#!/usr/bin/env bash
# ADR-005 / DB-M04 guardrail: the Matrix identity/federation config must never
# hardcode a bare IPv4 as `server_name` or well-known `m.server`. Those values
# are immutable inside every user/room ID, so an IP there bakes the host address
# into all MXIDs and breaks any server move. Server names must come from
# `.Values.matrix.matrixServerName` (per-environment overlay), never a literal IP.
#
# Static source scan (no helm / no secrets needed — this chart ships no committed
# values.yaml). Scans templates + any tracked values files for an IPv4 sitting in
# a `server_name` (yaml or nginx directive) or a well-known `m.server`.
set -euo pipefail

CHART_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$CHART_DIR"

# IPv4 immediately after `server_name:` / `server_name ` or inside `"m.server": "..."`.
pattern='server_name[ :][^#]*[0-9]{1,3}(\.[0-9]{1,3}){3}|"m\.server":[[:space:]]*"[0-9]{1,3}(\.[0-9]{1,3}){3}'

# Search template sources and any committed values files. Loopback/CIDR in other
# keys (e.g. exempt_from_ratelimiting) are out of scope — only server_name/m.server.
# Build the target list explicitly so a missing path is a loud error, not silence,
# and let grep's stderr through so real failures stay diagnosable.
targets=(templates values.yaml.default)
for overlay in values-*.yaml; do
  [ -e "$overlay" ] && targets+=("$overlay")
done

set +e
offenders="$(grep -rEn "$pattern" "${targets[@]}")"
status=$?
set -e
# grep: 0 = match found, 1 = no match (the good case), >1 = real error.
if [ "$status" -gt 1 ]; then
  echo "❌ guardrail could not scan the chart sources (grep exit $status)" >&2
  exit "$status"
fi

if [ -n "$offenders" ]; then
  echo "❌ ADR-005 guardrail: bare IPv4 in a Matrix server_name / m.server:" >&2
  printf '%s\n' "$offenders" >&2
  echo "Use .Values.matrix.matrixServerName (a domain) instead — see ADR-005 / DB-M04." >&2
  exit 1
fi

echo "✅ ADR-005 guardrail: no bare-IP server_name / m.server in chart sources."
