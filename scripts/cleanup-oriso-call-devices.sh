#!/usr/bin/env bash
#
# Remove disposable Matrix devices created by the retired Element Call SPA
# embedding. Dry-run is the default. The script only ever selects device IDs
# beginning with the exact ORISO_CALL_ prefix and requires an explicit,
# deliberately long confirmation phrase before deletion.
#
# Required environment:
#   SYNAPSE_ADMIN_URL    Synapse base URL reachable by the operator.
#   SYNAPSE_ADMIN_TOKEN  Server-admin access token. Never pass it as an argument.
#
# Usage:
#   scripts/cleanup-oriso-call-devices.sh --users-file matrix-users.txt
#
#   scripts/cleanup-oriso-call-devices.sh \
#     --users-file matrix-users.txt \
#     --apply \
#     --confirm DELETE_DISPOSABLE_ORISO_CALL_DEVICES
#
# The users file contains one fully-qualified local Matrix user ID per line.
# Blank lines and lines beginning with # are ignored. Supplying an explicit
# list keeps the destructive scope reviewable and avoids server-wide discovery.

set -euo pipefail

MODE="dry-run"
USERS_FILE=""
CONFIRMATION=""
EXPECTED_CONFIRMATION="DELETE_DISPOSABLE_ORISO_CALL_DEVICES"

die() {
  echo "error: $*" >&2
  exit 2
}

usage() {
  sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --users-file)
      USERS_FILE="${2:?--users-file requires a path}"
      shift 2
      ;;
    --apply)
      MODE="apply"
      shift
      ;;
    --confirm)
      CONFIRMATION="${2:?--confirm requires the confirmation phrase}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

command -v curl >/dev/null || die "curl is required"
command -v jq >/dev/null || die "jq is required"

: "${SYNAPSE_ADMIN_URL:?set SYNAPSE_ADMIN_URL}"
: "${SYNAPSE_ADMIN_TOKEN:?set SYNAPSE_ADMIN_TOKEN}"
[[ -n "${USERS_FILE}" ]] || die "--users-file is required"
[[ -r "${USERS_FILE}" ]] || die "users file is not readable"

if [[ "${MODE}" == "apply" && "${CONFIRMATION}" != "${EXPECTED_CONFIRMATION}" ]]; then
  die "--apply requires --confirm ${EXPECTED_CONFIRMATION}"
fi

SYNAPSE_ADMIN_URL="${SYNAPSE_ADMIN_URL%/}"
matched_total=0
deleted_total=0
user_count=0

while IFS= read -r user_id || [[ -n "${user_id}" ]]; do
  [[ -z "${user_id}" || "${user_id}" == \#* ]] && continue
  [[ "${user_id}" =~ ^@[^:]+:.+$ ]] || die "invalid Matrix user ID in users file"
  user_count=$((user_count + 1))

  encoded_user="$(jq -rn --arg value "${user_id}" '$value | @uri')"
  devices_response="$(
    curl -fsS --max-time 30 \
      -H "Authorization: Bearer ${SYNAPSE_ADMIN_TOKEN}" \
      "${SYNAPSE_ADMIN_URL}/_synapse/admin/v2/users/${encoded_user}/devices"
  )"
  devices="$(
    jq -ce \
      '[.devices[]?.device_id | select(type == "string" and startswith("ORISO_CALL_"))]' \
      <<<"${devices_response}"
  )"
  count="$(jq 'length' <<<"${devices}")"
  matched_total=$((matched_total + count))

  if [[ "${MODE}" == "apply" && "${count}" -gt 0 ]]; then
    payload="$(jq -cn --argjson devices "${devices}" '{devices: $devices}')"
    curl -fsS --max-time 30 \
      -X POST \
      -H "Authorization: Bearer ${SYNAPSE_ADMIN_TOKEN}" \
      -H "Content-Type: application/json" \
      --data "${payload}" \
      "${SYNAPSE_ADMIN_URL}/_synapse/admin/v2/users/${encoded_user}/delete_devices" \
      >/dev/null
    deleted_total=$((deleted_total + count))
  fi
done <"${USERS_FILE}"

[[ "${user_count}" -gt 0 ]] || die "users file contains no Matrix user IDs"

if [[ "${MODE}" == "dry-run" ]]; then
  echo "Dry-run: ${matched_total} disposable ORISO_CALL_ device(s) matched across ${user_count} user(s)."
  echo "No devices were deleted."
else
  echo "Applied: ${deleted_total} disposable ORISO_CALL_ device(s) deleted across ${user_count} user(s)."
fi
