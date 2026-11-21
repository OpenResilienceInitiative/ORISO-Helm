#!/usr/bin/env bash
# Integration test for scripts/cleanup-expired-matrix-puppet-tokens.sh against a
# throwaway Postgres with Synapse's access_tokens shape (needs docker).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="${ROOT}/scripts/cleanup-expired-matrix-puppet-tokens.sh"
PG="oriso-puppet-token-test-$$"
WORK="$(mktemp -d)"
cleanup() { docker rm -f "${PG}" >/dev/null 2>&1 || true; rm -rf "${WORK}"; }
trap cleanup EXIT

docker run -d --name "${PG}" -e POSTGRES_USER=synapse -e POSTGRES_DB=synapse \
  -e POSTGRES_PASSWORD=fixture-database-only postgres:16-alpine >/dev/null
for _ in $(seq 1 60); do
  docker exec "${PG}" pg_isready -U synapse -d synapse >/dev/null 2>&1 && break
  sleep 1
done
export PSQL_CMD="docker exec -i ${PG} psql -X -v ON_ERROR_STOP=1 -At -U synapse -d synapse"
q() { bash -c "${PSQL_CMD}" <<<"$1"; }

now=$(( $(date +%s) * 1000 ))
day=$((24 * 60 * 60 * 1000))
q "CREATE TABLE refresh_tokens (id BIGINT PRIMARY KEY);
CREATE TABLE access_tokens (
  id BIGINT PRIMARY KEY, user_id TEXT NOT NULL, device_id TEXT, token TEXT NOT NULL UNIQUE,
  valid_until_ms BIGINT, puppets_user_id TEXT, last_validated BIGINT,
  refresh_token_id BIGINT REFERENCES refresh_tokens (id) ON DELETE CASCADE, used BOOLEAN);
INSERT INTO refresh_tokens VALUES (1);
INSERT INTO access_tokens (id, user_id, device_id, token, valid_until_ms, puppets_user_id, refresh_token_id) VALUES
  (1, '@a:x', NULL, 't1', ${now} - 2 * ${day}, '@admin:x', NULL),
  (2, '@a:x', NULL, 't2', ${now} - 3 * ${day}, '@admin:x', NULL),
  (3, '@b:x', NULL, 't3', ${now} - 9 * ${day}, '@admin:x', NULL),
  (4, '@b:x', 'DEV', 't4', ${now} - 9 * ${day}, '@admin:x', NULL),
  (5, '@b:x', NULL, 't5', ${now} - 3600000, '@admin:x', NULL),
  (6, '@b:x', NULL, 't6', ${now} + 600000, '@admin:x', NULL),
  (7, '@c:x', 'WEB', 't7', ${now} - 9 * ${day}, NULL, NULL),
  (8, '@c:x', NULL, 't8', ${now} - 9 * ${day}, '@admin:x', 1);"

out="$("${SCRIPT}")"
grep -q "expired puppet tokens selected: 3 " <<<"${out}" || { echo "${out}"; echo "FAIL dry-run count"; exit 1; }
[[ "$(q 'SELECT count(*) FROM access_tokens')" == 8 ]] || { echo "FAIL dry-run deleted rows"; exit 1; }

if "${SCRIPT}" --apply --backup-dir "${WORK}" >/dev/null 2>&1; then echo "FAIL apply without phrase"; exit 1; fi
if "${SCRIPT}" --apply --confirm DELETE_EXPIRED_MATRIX_PUPPET_TOKENS >/dev/null 2>&1; then
  echo "FAIL apply without backup dir"; exit 1
fi

"${SCRIPT}" --apply --confirm DELETE_EXPIRED_MATRIX_PUPPET_TOKENS --backup-dir "${WORK}" \
  --batch-size 2 --pause 0 >"${WORK}/apply.log"
[[ "$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM access_tokens")" == "4,5,6,7,8" ]] \
  || { cat "${WORK}/apply.log"; echo "FAIL wrong rows deleted"; exit 1; }
backup="$(ls "${WORK}"/access_tokens-expired-puppet-*.csv.gz)"
[[ "$(gunzip -c "${backup}" | wc -l | tr -d ' ')" == 4 ]] || { echo "FAIL backup rows"; exit 1; }
[[ "$(stat -f %Lp "${backup}" 2>/dev/null || stat -c %a "${backup}")" == 600 ]] || { echo "FAIL backup mode"; exit 1; }

# Restore from the backup brings the rows back.
gunzip -c "${backup}" | docker exec -i "${PG}" psql -X -v ON_ERROR_STOP=1 -q -U synapse -d synapse \
  -c "\\copy access_tokens ($(cat "${backup%.csv.gz}.columns")) FROM STDIN CSV HEADER"
[[ "$(q 'SELECT count(*) FROM access_tokens')" == 8 ]] || { echo "FAIL restore"; exit 1; }

echo "matrix puppet token cleanup: ok"
