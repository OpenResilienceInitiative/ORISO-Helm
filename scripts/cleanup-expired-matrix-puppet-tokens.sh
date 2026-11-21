#!/usr/bin/env bash
#
# Remove expired Synapse admin "login as user" tokens (ORISO-Helm#628).
#
# UserService minted one such token per server-side Matrix action until
# ORISO-UserService#1756. Synapse keeps every row in access_tokens and never
# deletes expired ones (Dev, 2026-11-21: 117,643 rows, 45 MB). There is no
# Admin API for them, so this script works on the database directly.
#
# Selected rows, and only these:
#   puppets_user_id IS NOT NULL      admin "login as user" tokens
#   device_id IS NULL                no device is attached
#   refresh_token_id IS NULL         no refresh token refers to them
#   valid_until_ms < now - 24 h      expired for at least a day
#
# Dry-run is the default: it prints counts only. --apply needs the exact
# confirmation phrase, first writes a backup of every selected row (CSV with
# header, gzip, mode 600), verifies the backup row count, and only then
# deletes in small batches, limited to the ids that are in the backup.
#
# Usage (on the Dev host, KUBECONFIG set):
#   scripts/cleanup-expired-matrix-puppet-tokens.sh
#   scripts/cleanup-expired-matrix-puppet-tokens.sh --apply \
#     --confirm DELETE_EXPIRED_MATRIX_PUPPET_TOKENS \
#     --backup-dir /root/backups/synapse-access-tokens
#
# Options:
#   --namespace NS     Kubernetes namespace (default caritas)
#   --pod POD          Postgres pod (default matrix-postgres-0)
#   --batch-size N     rows per DELETE transaction (default 5000)
#   --pause SECONDS    pause between batches (default 1)
#
# PSQL_CMD may replace the kubectl exec call (tests): a shell command that
# reads SQL on stdin and runs psql with -X -v ON_ERROR_STOP=1 -At.
#
# Restore (rows are inert: all expired and without a device):
#   gunzip -c <backup>.csv.gz | kubectl -n caritas exec -i matrix-postgres-0 -- \
#     sh -c 'psql -U "$POSTGRES_USER" -d "${POSTGRES_DB:-synapse}" \
#       -c "\copy access_tokens ($(cat <backup>.columns)) FROM STDIN CSV HEADER"'

set -euo pipefail

MODE="dry-run"
CONFIRMATION=""
EXPECTED_CONFIRMATION="DELETE_EXPIRED_MATRIX_PUPPET_TOKENS"
NAMESPACE="caritas"
POD="matrix-postgres-0"
BACKUP_DIR=""
BATCH_SIZE=5000
PAUSE=1
MARGIN_MS=$((24 * 60 * 60 * 1000))

die() {
  echo "error: $*" >&2
  exit 2
}

usage() {
  sed -n '2,42p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) MODE="apply"; shift ;;
    --confirm) CONFIRMATION="${2:?--confirm requires the phrase}"; shift 2 ;;
    --backup-dir) BACKUP_DIR="${2:?--backup-dir requires a path}"; shift 2 ;;
    --namespace) NAMESPACE="${2:?}"; shift 2 ;;
    --pod) POD="${2:?}"; shift 2 ;;
    --batch-size) BATCH_SIZE="${2:?}"; shift 2 ;;
    --pause) PAUSE="${2:?}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ "${BATCH_SIZE}" =~ ^[0-9]+$ && "${BATCH_SIZE}" -ge 1 && "${BATCH_SIZE}" -le 50000 ]] \
  || die "--batch-size must be 1..50000"
[[ "${PAUSE}" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "--pause must be a number of seconds"
if [[ "${MODE}" == "apply" ]]; then
  [[ "${CONFIRMATION}" == "${EXPECTED_CONFIRMATION}" ]] \
    || die "--apply requires --confirm ${EXPECTED_CONFIRMATION}"
  [[ -n "${BACKUP_DIR}" ]] || die "--apply requires --backup-dir"
fi

psql_run() {
  if [[ -n "${PSQL_CMD:-}" ]]; then
    bash -c "${PSQL_CMD}"
  else
    kubectl -n "${NAMESPACE}" exec -i "${POD}" -- \
      sh -c 'exec psql -X -v ON_ERROR_STOP=1 -At -U "$POSTGRES_USER" -d "${POSTGRES_DB:-synapse}"'
  fi
}

CUTOFF_MS=$(( $(date +%s) * 1000 - MARGIN_MS ))
WHERE="puppets_user_id IS NOT NULL AND device_id IS NULL AND refresh_token_id IS NULL
  AND valid_until_ms IS NOT NULL AND valid_until_ms < ${CUTOFF_MS}"

summary="$(psql_run <<SQL
SELECT 'selected', count(*), coalesce(max(id), 0) FROM access_tokens WHERE ${WHERE};
SELECT 'table_rows', count(*), 0 FROM access_tokens;
SQL
)"
SELECTED="$(awk -F'|' '$1=="selected"{print $2}' <<<"${summary}")"
MAX_ID="$(awk -F'|' '$1=="selected"{print $3}' <<<"${summary}")"
TABLE_ROWS="$(awk -F'|' '$1=="table_rows"{print $2}' <<<"${summary}")"
[[ "${SELECTED}" =~ ^[0-9]+$ && "${MAX_ID}" =~ ^[0-9]+$ ]] || die "could not read counts"

echo "access_tokens rows: ${TABLE_ROWS}"
echo "expired puppet tokens selected: ${SELECTED} (expired before $(date -u -r $((CUTOFF_MS / 1000)) +%FT%TZ 2>/dev/null || date -u -d @$((CUTOFF_MS / 1000)) +%FT%TZ))"
echo "per expiry day:"
psql_run <<SQL | sed 's/^/  /'
SELECT to_timestamp(valid_until_ms / 1000)::date, count(*) FROM access_tokens
WHERE ${WHERE} GROUP BY 1 ORDER BY 1;
SQL

if [[ "${MODE}" != "apply" ]]; then
  echo "dry-run: nothing written, nothing deleted"
  exit 0
fi
[[ "${SELECTED}" -gt 0 ]] || { echo "nothing to delete"; exit 0; }

# Backup: every selected row up to MAX_ID, with header, before any delete.
umask 077
mkdir -p "${BACKUP_DIR}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="${BACKUP_DIR}/access_tokens-expired-puppet-${stamp}.csv.gz"
columns="$(psql_run <<'SQL'
SELECT string_agg(quote_ident(column_name), ',' ORDER BY ordinal_position)
FROM information_schema.columns WHERE table_name = 'access_tokens' AND table_schema = current_schema();
SQL
)"
[[ -n "${columns}" ]] || die "could not read access_tokens columns"
printf '%s\n' "${columns}" > "${backup%.csv.gz}.columns"
psql_run <<SQL | gzip -9 > "${backup}"
COPY (SELECT ${columns} FROM access_tokens WHERE ${WHERE} AND id <= ${MAX_ID} ORDER BY id) TO STDOUT WITH CSV HEADER;
SQL
gzip -t "${backup}" || die "backup is not a valid gzip file: ${backup}"
backed_up=$(( $(gunzip -c "${backup}" | wc -l) - 1 ))
[[ "${backed_up}" -eq "${SELECTED}" ]] \
  || die "backup has ${backed_up} rows, expected ${SELECTED}; nothing deleted (${backup})"
echo "backup: ${backup} (${backed_up} rows)"

# Delete in batches, only ids covered by the backup.
deleted_total=0
while :; do
  deleted="$(psql_run <<SQL
WITH doomed AS (
  SELECT id FROM access_tokens WHERE ${WHERE} AND id <= ${MAX_ID} ORDER BY id LIMIT ${BATCH_SIZE}
), gone AS (
  DELETE FROM access_tokens WHERE id IN (SELECT id FROM doomed) RETURNING 1
)
SELECT count(*) FROM gone;
SQL
)"
  [[ "${deleted}" =~ ^[0-9]+$ ]] || die "unexpected delete result after ${deleted_total} rows"
  deleted_total=$((deleted_total + deleted))
  [[ "${deleted}" -gt 0 ]] || break
  echo "deleted ${deleted_total} / ${SELECTED}"
  sleep "${PAUSE}"
done
echo "done: deleted ${deleted_total} rows; backup ${backup}"
