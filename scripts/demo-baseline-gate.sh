#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASELINE_DIR="${ROOT_DIR}/demo-baseline"
SYNC_SQL="${BASELINE_DIR}/demo-baseline-sync.sql"
CHECK_SQL="${BASELINE_DIR}/demo-baseline-check.sql"
MANIFEST_JSON="${BASELINE_DIR}/manifest.json"

NAMESPACE="${NAMESPACE:-caritas}"
MARIADB_SECRET="${MARIADB_SECRET:-mariadb-secret}"
MARIADB_POD_SELECTOR="${MARIADB_POD_SELECTOR:-app=mariadb}"
MARIADB_USER="${MARIADB_USER:-root}"

usage() {
  cat <<'USAGE'
Usage: scripts/demo-baseline-gate.sh <check|sync|smoke|all>

Environment:
  NAMESPACE              Kubernetes namespace, default: caritas
  MARIADB_SECRET         Secret containing MYSQL_ROOT_PASSWORD, default: mariadb-secret
  MARIADB_POD_SELECTOR   MariaDB pod selector, default: app=mariadb
  MARIADB_USER           MariaDB user, default: root
  MARIADB_PASSWORD       Optional; otherwise read from Kubernetes secret
  DEMO_BASE_URL          Required for smoke/all, for example https://api.oriso.org
USAGE
}

require_tool() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required tool: $1" >&2
    exit 2
  fi
}

mariadb_password() {
  if [[ -n "${MARIADB_PASSWORD:-}" ]]; then
    printf '%s' "${MARIADB_PASSWORD}"
    return
  fi

  kubectl -n "${NAMESPACE}" get secret "${MARIADB_SECRET}" \
    -o jsonpath='{.data.MYSQL_ROOT_PASSWORD}' | base64 --decode
}

mariadb_pod() {
  kubectl -n "${NAMESPACE}" get pod -l "${MARIADB_POD_SELECTOR}" \
    -o jsonpath='{.items[0].metadata.name}'
}

run_sql_file() {
  local sql_file="$1"
  local pod password
  pod="$(mariadb_pod)"
  password="$(mariadb_password)"

  if [[ -z "${pod}" ]]; then
    echo "No MariaDB pod found for selector ${MARIADB_POD_SELECTOR} in namespace ${NAMESPACE}" >&2
    exit 2
  fi

  kubectl -n "${NAMESPACE}" exec -i "${pod}" -- \
    mariadb --batch --raw --skip-column-names -u"${MARIADB_USER}" -p"${password}" < "${sql_file}"
}

sync_baseline() {
  require_tool kubectl
  run_sql_file "${SYNC_SQL}" >/dev/null
  echo "Demo baseline sync applied."
}

check_baseline() {
  require_tool kubectl
  local drift
  drift="$(run_sql_file "${CHECK_SQL}")"
  if [[ -n "${drift}" ]]; then
    echo "Demo baseline drift detected:" >&2
    echo "${drift}" >&2
    exit 1
  fi
  echo "Demo baseline drift check passed."
}

smoke_baseline() {
  require_tool curl
  require_tool python3

  if [[ -z "${DEMO_BASE_URL:-}" ]]; then
    echo "DEMO_BASE_URL is required for smoke checks, for example https://api.oriso.org" >&2
    exit 2
  fi

  local failures=0
  while read -r postcode topic_id consulting_type; do
    local url body
    url="${DEMO_BASE_URL%/}/service/agencies?postcode=${postcode}&topicId=${topic_id}&consultingType=${consulting_type}"
    if ! body="$(curl -fsS --max-time 20 "${url}")"; then
      echo "Demo baseline smoke check request failed: ${url}" >&2
      failures=$((failures + 1))
      continue
    fi
    if [[ -z "${body}" || "${body}" == "[]" ]]; then
      echo "Demo baseline smoke check returned no visible agency: ${url}" >&2
      failures=$((failures + 1))
    fi
  done < <(python3 - "${MANIFEST_JSON}" <<'PY'
import json
import sys

manifest_path = sys.argv[1]
with open(manifest_path, encoding="utf-8") as handle:
    manifest = json.load(handle)

for check in manifest["visibilityChecks"]:
    print(check["postcode"], check["topicId"], check["consultingType"])
PY
)

  if [[ "${failures}" -gt 0 ]]; then
    exit 1
  fi

  echo "Demo baseline public registration smoke check passed."
}

main() {
  local action="${1:-}"
  case "${action}" in
    sync)
      sync_baseline
      ;;
    check)
      check_baseline
      ;;
    smoke)
      smoke_baseline
      ;;
    all)
      sync_baseline
      check_baseline
      smoke_baseline
      ;;
    -h|--help|help|"")
      usage
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
}

main "$@"
