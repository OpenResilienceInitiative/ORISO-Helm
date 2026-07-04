#!/usr/bin/env bash
#
# liquibase-baseline-sync.sh — one-time Liquibase baseline for an EXISTING
# ORISO environment before the Helm default flips Liquibase back on (package L3).
#
# See runbooks/liquibase-baseline-sync.md for the full operator runbook,
# including the decision matrix that tells you WHICH mode to use per environment.
#
# What it does (per service database):
#   1. status --verbose           (read-only: list pending changesets)
#   2. clear-checksums            (historical checksets were edited over time)
#   3. changelog-sync             (mark already-applied changesets as ran)
#      — or, with --mark-ran-count N, mark only the first N pending changesets
#        as ran so that a subsequent update genuinely applies the remainder
#   4. status --verbose           (expect zero pending, or the intentional rest)
#   5. update                     (no-op proof, or applies the intentional rest)
#
# Mechanism: the official liquibase/liquibase Docker image, pinned to the same
# version as the services liquibase-core (4.23.2), with the service repo
# src/main/resources mounted read-only. --search-path is set so that recorded
# FILENAME values stay "db/changelog/changeset/..." — identical to what the
# Spring Boot runtime records. Never change this, changeset identity is
# (id, author, filepath).
#
# Credentials are passed exclusively via environment variables — never
# hardcode them here or in CI files (see ORISO-Database docs/secret-management.md).
#
# Required environment variables:
#   LB_DB_HOST        database host as reachable FROM the liquibase container
#   LB_DB_USERNAME    database user
#   LB_DB_PASSWORD    database password
#   LB_CHANGELOG_DIR  absolute path to the service repo src/main/resources
#                     (must contain db/changelog/<service>-master.xml)
#
# Optional environment variables:
#   LB_DB_PORT         database port (default: 3306)
#   LB_DB_NAME         database name (default: the service name)
#   LB_LIQUIBASE_IMAGE liquibase Docker image (default: liquibase/liquibase:4.23.2)
#   LB_DOCKER_NETWORK  Docker network for the liquibase container (e.g. a
#                      user-defined network shared with a local MariaDB
#                      container, or "host" on Linux hosts)
#
# Usage:
#   liquibase-baseline-sync.sh <service> [--execute] [--mark-ran-count N] [--skip-update] [--fresh]
#
#   <service>            one of: tenantservice userservice agencyservice consultingtypeservice
#   --execute            actually run the mutating steps. Without it the script
#                        is a DRY RUN: it runs only the read-only status step
#                        and prints the commands it would run.
#   --mark-ran-count N   instead of a full changelog-sync, mark only the first
#                        N pending changesets as ran (mark-next-changeset-ran
#                        loop). Use for dump-provisioned or uncertain
#                        environments where the tail of the changelog is
#                        genuinely missing from the schema.
#   --skip-update        stop after the post-sync status (skip step 5).
#   --fresh              throwaway/local mode: skip clear-checksums and
#                        changelog-sync entirely and just run update against an
#                        empty database (drop & recreate beforehand yourself).
#
set -euo pipefail

SERVICES=(tenantservice userservice agencyservice consultingtypeservice)

usage() {
  cat <<'EOF'
Usage: liquibase-baseline-sync.sh <service> [--execute] [--mark-ran-count N] [--skip-update] [--fresh]

  <service>            one of: tenantservice userservice agencyservice consultingtypeservice
  --execute            actually run the mutating steps (default is DRY RUN:
                       only the read-only status step runs, mutating commands
                       are printed instead)
  --mark-ran-count N   selective baseline: mark only the first N pending
                       changesets as ran instead of a full changelog-sync
  --skip-update        stop after the post-sync status check
  --fresh              throwaway/local mode: no sync, just update against an
                       empty (dropped & recreated) database

Required env vars: LB_DB_HOST, LB_DB_USERNAME, LB_DB_PASSWORD, LB_CHANGELOG_DIR
Optional env vars: LB_DB_PORT (3306), LB_DB_NAME (<service>),
                   LB_LIQUIBASE_IMAGE (liquibase/liquibase:4.23.2), LB_DOCKER_NETWORK

Credentials via env vars only — see ORISO-Database docs/secret-management.md.
Full procedure and decision matrix: runbooks/liquibase-baseline-sync.md
EOF
  exit 1
}

log() {
  printf '[%s] [baseline-sync] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

die() {
  log "ERROR: $*" >&2
  exit 1
}

# --- argument parsing -------------------------------------------------------

[[ $# -ge 1 ]] || usage

SERVICE="$1"
shift

EXECUTE=0
MARK_RAN_COUNT=""
SKIP_UPDATE=0
FRESH=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --execute) EXECUTE=1 ;;
    --mark-ran-count)
      [[ $# -ge 2 ]] || die "--mark-ran-count needs a value"
      MARK_RAN_COUNT="$2"
      shift
      ;;
    --skip-update) SKIP_UPDATE=1 ;;
    --fresh) FRESH=1 ;;
    -h|--help) usage ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

valid_service=0
for s in "${SERVICES[@]}"; do
  [[ "$SERVICE" == "$s" ]] && valid_service=1
done
[[ $valid_service -eq 1 ]] || die "unknown service '$SERVICE' (expected one of: ${SERVICES[*]})"

if [[ -n "$MARK_RAN_COUNT" && ! "$MARK_RAN_COUNT" =~ ^[0-9]+$ ]]; then
  die "--mark-ran-count must be a non-negative integer, got '$MARK_RAN_COUNT'"
fi
if [[ -n "$MARK_RAN_COUNT" && $FRESH -eq 1 ]]; then
  die "--mark-ran-count and --fresh are mutually exclusive"
fi

# --- environment validation --------------------------------------------------

: "${LB_DB_HOST:?LB_DB_HOST is required (database host reachable from the liquibase container)}"
: "${LB_DB_USERNAME:?LB_DB_USERNAME is required (never hardcode credentials — see ORISO-Database docs/secret-management.md)}"
: "${LB_DB_PASSWORD:?LB_DB_PASSWORD is required (never hardcode credentials — see ORISO-Database docs/secret-management.md)}"
: "${LB_CHANGELOG_DIR:?LB_CHANGELOG_DIR is required (service repo src/main/resources)}"

LB_DB_PORT="${LB_DB_PORT:-3306}"
LB_DB_NAME="${LB_DB_NAME:-$SERVICE}"
LB_LIQUIBASE_IMAGE="${LB_LIQUIBASE_IMAGE:-liquibase/liquibase:4.23.2}"
LB_DOCKER_NETWORK="${LB_DOCKER_NETWORK:-}"

MASTER_RELPATH="db/changelog/${SERVICE}-master.xml"

[[ -d "$LB_CHANGELOG_DIR" ]] || die "LB_CHANGELOG_DIR does not exist: $LB_CHANGELOG_DIR"
[[ -f "$LB_CHANGELOG_DIR/$MASTER_RELPATH" ]] \
  || die "master changelog not found: $LB_CHANGELOG_DIR/$MASTER_RELPATH (is LB_CHANGELOG_DIR the repo src/main/resources?)"
command -v docker >/dev/null 2>&1 || die "docker is required but not on PATH"

# --- liquibase invocation -----------------------------------------------------

DOCKER_ARGS=(run --rm)
if [[ -n "$LB_DOCKER_NETWORK" ]]; then
  DOCKER_ARGS+=(--network "$LB_DOCKER_NETWORK")
fi
DOCKER_ARGS+=(
  -v "$LB_CHANGELOG_DIR:/liquibase/changelog:ro"
  -e LIQUIBASE_COMMAND_URL="jdbc:mariadb://${LB_DB_HOST}:${LB_DB_PORT}/${LB_DB_NAME}"
  -e LIQUIBASE_COMMAND_USERNAME="$LB_DB_USERNAME"
  -e LIQUIBASE_COMMAND_PASSWORD="$LB_DB_PASSWORD"
  -e LIQUIBASE_COMMAND_CHANGELOG_FILE="$MASTER_RELPATH"
  -e LIQUIBASE_SEARCH_PATH="/liquibase/changelog"
  -e LIQUIBASE_SHOW_BANNER="false"
  "$LB_LIQUIBASE_IMAGE"
)

lb() {
  log "liquibase $*"
  docker "${DOCKER_ARGS[@]}" "$@"
}

would() {
  log "DRY RUN — would run: liquibase $*"
}

# --- run ----------------------------------------------------------------------

log "service=$SERVICE db=${LB_DB_HOST}:${LB_DB_PORT}/${LB_DB_NAME} image=$LB_LIQUIBASE_IMAGE"
log "changelog=$LB_CHANGELOG_DIR/$MASTER_RELPATH"
if [[ $EXECUTE -eq 0 ]]; then
  log "DRY RUN mode (default). Pass --execute to run the mutating steps."
fi

log "step 1/5: status (read-only) — pending changesets before baseline"
lb status --verbose

if [[ $FRESH -eq 1 ]]; then
  log "--fresh: skipping clear-checksums and changelog-sync (empty/recreated database expected)"
else
  log "step 2/5: clear-checksums"
  if [[ $EXECUTE -eq 1 ]]; then
    lb clear-checksums
  else
    would clear-checksums
  fi

  if [[ -n "$MARK_RAN_COUNT" ]]; then
    log "step 3/5: mark-next-changeset-ran x $MARK_RAN_COUNT (selective baseline)"
    if [[ $EXECUTE -eq 1 ]]; then
      i=0
      while [[ $i -lt $MARK_RAN_COUNT ]]; do
        i=$((i + 1))
        log "mark-next-changeset-ran ($i/$MARK_RAN_COUNT)"
        lb mark-next-changeset-ran
      done
    else
      would "mark-next-changeset-ran ($MARK_RAN_COUNT times)"
    fi
  else
    log "step 3/5: changelog-sync (mark ALL pending changesets as ran)"
    if [[ $EXECUTE -eq 1 ]]; then
      lb changelog-sync
    else
      would changelog-sync
    fi
  fi
fi

log "step 4/5: status (read-only) — pending changesets after baseline"
if [[ $EXECUTE -eq 1 || $FRESH -eq 1 ]]; then
  lb status --verbose
else
  would "status --verbose"
fi

if [[ $SKIP_UPDATE -eq 1 ]]; then
  log "step 5/5 skipped (--skip-update)"
elif [[ $EXECUTE -eq 1 ]]; then
  log "step 5/5: update (no-op proof, or applies intentionally-pending changesets)"
  lb update
else
  would update
fi

log "done: $SERVICE"
