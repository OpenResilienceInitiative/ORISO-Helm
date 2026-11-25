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
# deletes in small batches by the exact IDs in the backup. Apply requires Python 3.
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
  command -v python3 >/dev/null || die "--apply requires Python 3 to verify the CSV backup"
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
# Reserve a unique run ID so concurrent operators cannot overwrite the same archive.
id_batches="$(mktemp "${BACKUP_DIR}/.access-token-batches.XXXXXXXX")"
trap 'rm -f -- "${id_batches}"' EXIT
backup="${BACKUP_DIR}/access_tokens-expired-puppet-${stamp}-${id_batches##*.}.csv.gz"
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
# Parse the archived CSV, including quoted fields with line breaks, before issuing any DELETE.
python3 - "${backup}" "${SELECTED}" "${MAX_ID}" "${BATCH_SIZE}" >"${id_batches}" <<'PYTHON'
import csv
import gzip
import sys

archive, expected, max_id, batch_size = sys.argv[1:]
expected, max_id, batch_size = int(expected), int(max_id), int(batch_size)
with gzip.open(archive, "rt", encoding="utf-8", newline="") as source:
    rows = csv.DictReader(source, strict=True)
    if not rows.fieldnames or rows.fieldnames.count("id") != 1:
        raise SystemExit("backup must have one id column")
    count = 0
    previous_id = -1
    batch = []
    for row in rows:
        value = row["id"]
        if None in row or None in row.values() or not value.isascii() or not value.isdecimal():
            raise SystemExit("backup contains an invalid access-token row")
        row_id = int(value)
        if not previous_id < row_id <= max_id:
            raise SystemExit("backup access-token IDs are unordered, repeated or out of range")
        previous_id = row_id
        count += 1
        batch.append(str(row_id))
        if len(batch) == batch_size:
            print(",".join(batch))
            batch.clear()
    if count != expected:
        raise SystemExit(f"backup has {count} rows, expected {expected}; nothing deleted")
    if batch:
        print(",".join(batch))
PYTHON
echo "backup: ${backup} (${SELECTED} rows)"

# Recheck eligibility at deletion and visit each verified batch even if another run removed one.
deleted_total=0
while IFS= read -r batch_ids; do
  deleted="$(psql_run <<SQL
WITH gone AS (
  DELETE FROM access_tokens WHERE ${WHERE} AND id IN (${batch_ids}) RETURNING 1
)
SELECT count(*) FROM gone;
SQL
)"
  [[ "${deleted}" =~ ^[0-9]+$ ]] || die "unexpected delete result after ${deleted_total} rows"
  deleted_total=$((deleted_total + deleted))
  echo "deleted ${deleted_total} / ${SELECTED}"
  sleep "${PAUSE}"
done < "${id_batches}"
echo "done: deleted ${deleted_total} rows; backup ${backup}"
