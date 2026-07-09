#!/usr/bin/env bash
# ADR-005 / DB-M04 guardrail: the Matrix identity/federation config must never
# hardcode a bare IPv4 as `server_name` or well-known `m.server`. Those values
# are immutable inside every user/room ID, so an IP there bakes the host address
# into all MXIDs and breaks any server move. Server names must come from
# `.Values.matrix.matrixServerName` (per-environment overlay), never a literal IP.
#
# Runs `helm template` with the default values and fails if any rendered
# `server_name:` / `server_name <x>;` / `"m.server"` line contains an IPv4.
set -euo pipefail

CHART_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VALUES="${1:-$CHART_DIR/values.yaml.default}"

if ! command -v helm >/dev/null 2>&1; then
  echo "helm not found — cannot render templates" >&2
  exit 2
fi

rendered="$(helm template oriso "$CHART_DIR" -f "$VALUES" 2>/dev/null)"

# IPv4 in a server_name (Synapse yaml or nginx directive) or a well-known m.server.
# Excludes the trusted_key_servers matrix.org line and CIDR/loopback which are not identities.
offenders="$(printf '%s\n' "$rendered" \
  | grep -nE 'server_name[ :].*[0-9]{1,3}(\.[0-9]{1,3}){3}|"m\.server":[[:space:]]*"[0-9]{1,3}(\.[0-9]{1,3}){3}' \
  || true)"

if [ -n "$offenders" ]; then
  echo "❌ ADR-005 guardrail: bare IPv4 found in a Matrix server_name / m.server:" >&2
  printf '%s\n' "$offenders" >&2
  echo "Use .Values.matrix.matrixServerName (a domain) instead — see ADR-005 / DB-M04." >&2
  exit 1
fi

echo "✅ ADR-005 guardrail: no bare-IP server_name / m.server in rendered manifests."
