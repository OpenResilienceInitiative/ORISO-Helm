#!/usr/bin/env bash
#
# seed-keycloak-users.sh — bulk-create test users in Keycloak via the Admin API.
#
# Why this exists
#   OTP/2FA in ORISO is enforced at *interactive login* only. Users created
#   through the Keycloak Admin API never receive a CONFIGURE_TOTP required
#   action, so they can log in (or fetch a direct-grant token) with just
#   username + password. This is the supported way to rapidly create many
#   test users. See docs/infrastructure-report-2026-07.md §6.
#
# What it does
#   1. Obtains an admin token (direct grant, admin-cli, master realm).
#   2. Creates each requested user (enabled, emailVerified=true, password set,
#      no required actions) — skipping any that already exist.
#   3. Optionally assigns a realm role.
#   4. Optionally writes each newly created {username,password,...} back into a
#      SOPS-encrypted store so the credential is never lost (--write-back).
#
# It NEVER prints or commits credentials except into the encrypted store.
#
# Usage
#   Generate N users:
#     KEYCLOAK_URL=https://host/auth KEYCLOAK_REALM=online-beratung \
#     KEYCLOAK_ADMIN_USER=admin KEYCLOAK_ADMIN_PASSWORD=*** \
#       scripts/seed-keycloak-users.sh --count 10 --role consultant --prefix test-consultant
#
#   From an explicit users file (JSON list, e.g. the decrypted store):
#     scripts/seed-keycloak-users.sh --users-file <(sops -d test-data/test-users.enc.json)
#
#   With write-back into the encrypted store:
#     scripts/seed-keycloak-users.sh --count 5 --role user \
#       --write-back --store test-data/test-users.enc.json --env predev --tenant t1
#
# Environment (required)
#   KEYCLOAK_URL             Base URL incl. the /auth path (no trailing slash needed).
#   KEYCLOAK_REALM           Realm the users are created in (e.g. online-beratung).
#   KEYCLOAK_ADMIN_USER      Admin username.
#   KEYCLOAK_ADMIN_PASSWORD  Admin password (never pass on the command line).
#
# Environment (optional)
#   KEYCLOAK_ADMIN_REALM     Realm to authenticate the admin against (default: master).
#   KEYCLOAK_ADMIN_CLIENT    Admin client id (default: admin-cli).
#
set -euo pipefail

# ---------------------------------------------------------------------------
# defaults / args
# ---------------------------------------------------------------------------
COUNT=0
USERS_FILE=""
ROLE=""
PREFIX="test-user"
DEFAULT_PASSWORD=""          # if empty, a random password is generated per user
WRITE_BACK=0
STORE="test-data/test-users.enc.json"
ENVIRONMENT="predev"
TENANT=""
DRY_RUN=0

ADMIN_REALM="${KEYCLOAK_ADMIN_REALM:-master}"
ADMIN_CLIENT="${KEYCLOAK_ADMIN_CLIENT:-admin-cli}"

die() { echo "error: $*" >&2; exit 1; }
log() { echo "[seed] $*" >&2; }

usage() { sed -n '2,55p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --count)        COUNT="${2:?}"; shift 2 ;;
    --users-file)   USERS_FILE="${2:?}"; shift 2 ;;
    --role)         ROLE="${2:?}"; shift 2 ;;
    --prefix)       PREFIX="${2:?}"; shift 2 ;;
    --password)     DEFAULT_PASSWORD="${2:?}"; shift 2 ;;
    --write-back)   WRITE_BACK=1; shift ;;
    --store)        STORE="${2:?}"; shift 2 ;;
    --env)          ENVIRONMENT="${2:?}"; shift 2 ;;
    --tenant)       TENANT="${2:?}"; shift 2 ;;
    --dry-run)      DRY_RUN=1; shift ;;
    -h|--help)      usage 0 ;;
    *)              die "unknown argument: $1 (see --help)" ;;
  esac
done

# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------
command -v curl >/dev/null || die "curl is required"
command -v jq   >/dev/null || die "jq is required"

: "${KEYCLOAK_URL:?set KEYCLOAK_URL (e.g. https://host/auth)}"
: "${KEYCLOAK_REALM:?set KEYCLOAK_REALM (target realm for the users)}"
: "${KEYCLOAK_ADMIN_USER:?set KEYCLOAK_ADMIN_USER}"
: "${KEYCLOAK_ADMIN_PASSWORD:?set KEYCLOAK_ADMIN_PASSWORD}"

KEYCLOAK_URL="${KEYCLOAK_URL%/}"

if [[ "$COUNT" -eq 0 && -z "$USERS_FILE" ]]; then
  die "provide either --count N or --users-file FILE"
fi
if [[ "$WRITE_BACK" -eq 1 ]]; then
  command -v sops >/dev/null || die "--write-back needs 'sops' (https://github.com/getsops/sops)"
fi

gen_password() {
  # 24 URL-safe chars; prefers openssl, falls back to /dev/urandom.
  if command -v openssl >/dev/null; then
    openssl rand -base64 18 | tr -d '\n/+=' | cut -c1-24
  else
    LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24
  fi
}

# ---------------------------------------------------------------------------
# admin token
# ---------------------------------------------------------------------------
get_token() {
  local resp
  resp="$(curl -fsS \
    -d "client_id=${ADMIN_CLIENT}" \
    -d "username=${KEYCLOAK_ADMIN_USER}" \
    -d "password=${KEYCLOAK_ADMIN_PASSWORD}" \
    -d "grant_type=password" \
    "${KEYCLOAK_URL}/realms/${ADMIN_REALM}/protocol/openid-connect/token")" \
    || die "admin token request failed (check KEYCLOAK_URL / admin creds / realm)"
  echo "$resp" | jq -r '.access_token'
}

# ---------------------------------------------------------------------------
# build the work list as a JSON array: [{username,password,email,firstName,lastName,role}]
# ---------------------------------------------------------------------------
build_users_json() {
  if [[ -n "$USERS_FILE" ]]; then
    [[ -r "$USERS_FILE" ]] || die "users file not readable: $USERS_FILE"
    # Expect JSON: either a bare list or an object with a top-level 'users' list.
    # The decrypted store works directly: --users-file <(sops -d store.enc.json)
    jq 'if type == "array" then . else (.users // []) end' "$USERS_FILE" \
      || die "could not parse users file as JSON: $USERS_FILE"
  else
    local arr="[]" i u pw
    for ((i = 1; i <= COUNT; i++)); do
      u="${PREFIX}-$(printf '%03d' "$i")"
      pw="${DEFAULT_PASSWORD:-$(gen_password)}"
      arr="$(jq \
        --arg u "$u" --arg pw "$pw" --arg role "$ROLE" \
        '. + [{username:$u, password:$pw, email:($u + "@example.test"),
               firstName:"Test", lastName:$u, role:$role}]' <<<"$arr")"
    done
    echo "$arr"
  fi
}

user_exists() {  # $1 token, $2 username
  local n
  n="$(curl -fsS -H "Authorization: Bearer $1" \
        "${KEYCLOAK_URL}/admin/realms/${KEYCLOAK_REALM}/users?exact=true&username=$2" \
        | jq 'length')"
  [[ "$n" -gt 0 ]]
}

create_user() {  # $1 token, $2 user-json
  local token="$1" uj="$2" username password email first last payload
  username="$(jq -r '.username' <<<"$uj")"
  password="$(jq -r '.password' <<<"$uj")"
  email="$(jq -r '.email // (.username + "@example.test")' <<<"$uj")"
  first="$(jq -r '.firstName // "Test"' <<<"$uj")"
  last="$(jq -r '.lastName // .username' <<<"$uj")"

  payload="$(jq -n \
    --arg u "$username" --arg e "$email" --arg f "$first" --arg l "$last" --arg p "$password" \
    '{username:$u, email:$e, firstName:$f, lastName:$l,
      enabled:true, emailVerified:true, requiredActions:[],
      credentials:[{type:"password", value:$p, temporary:false}]}')"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY-RUN would create: $username"
    return 0
  fi

  curl -fsS -o /dev/null -X POST \
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
    -d "$payload" \
    "${KEYCLOAK_URL}/admin/realms/${KEYCLOAK_REALM}/users" \
    || die "failed to create user $username"
}

assign_role() {  # $1 token, $2 username, $3 role
  local token="$1" username="$2" role="$3" uid rolejson
  [[ -z "$role" || "$role" == "null" ]] && return 0
  uid="$(curl -fsS -H "Authorization: Bearer $token" \
        "${KEYCLOAK_URL}/admin/realms/${KEYCLOAK_REALM}/users?exact=true&username=${username}" \
        | jq -r '.[0].id')"
  [[ -n "$uid" && "$uid" != "null" ]] || { log "warn: cannot resolve id for $username, skipping role"; return 0; }
  rolejson="$(curl -fsS -H "Authorization: Bearer $token" \
        "${KEYCLOAK_URL}/admin/realms/${KEYCLOAK_REALM}/roles/${role}" 2>/dev/null || true)"
  [[ -n "$rolejson" && "$rolejson" != "null" ]] || { log "warn: realm role '$role' not found, skipping"; return 0; }
  curl -fsS -o /dev/null -X POST \
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
    -d "[$rolejson]" \
    "${KEYCLOAK_URL}/admin/realms/${KEYCLOAK_REALM}/users/${uid}/role-mappings/realm" \
    || log "warn: role assignment failed for $username"
}

# ---------------------------------------------------------------------------
# write-back into the SOPS-encrypted store
# ---------------------------------------------------------------------------
write_back() {  # $1 user-json
  local uj="$1" tmp out store_dir
  store_dir="$(dirname "$STORE")"
  mkdir -p "$store_dir"
  tmp="$(mktemp)"
  out="$(mktemp "${store_dir}/.$(basename "$STORE").XXXXXX")"
  trap 'rm -f "$tmp" "${tmp}.new" "$out"' RETURN
  if [[ -f "$STORE" ]]; then
    sops -d "$STORE" > "$tmp"
  else
    echo '{"users":[]}' > "$tmp"
  fi
  jq \
    --arg env "$ENVIRONMENT" --arg tenant "$TENANT" --argjson u "$uj" \
    '.users += [{
        env:      $env,
        tenant:   $tenant,
        role:     ($u.role // ""),
        username: $u.username,
        password: $u.password,
        created:  "seed-script"
    }]' "$tmp" > "${tmp}.new"
  mv "${tmp}.new" "$tmp"
  # Re-encrypt using the target store path so .sops.yaml creation rules match.
  sops --filename-override "$STORE" -e "$tmp" > "$out"
  mv "$out" "$STORE"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
main() {
  local token="" users_json count created=0 written=0
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY-RUN: not contacting Keycloak"
  else
    token="$(get_token)"
    [[ -n "$token" && "$token" != "null" ]] || die "empty admin token"
  fi
  users_json="$(build_users_json)"
  count="$(jq 'length' <<<"$users_json")"
  log "processing ${count} user(s) in realm '${KEYCLOAK_REALM}'"

  for ((idx = 0; idx < count; idx++)); do
    local uj username
    uj="$(jq -c ".[$idx]" <<<"$users_json")"
    username="$(jq -r '.username' <<<"$uj")"
    [[ -n "$username" && "$username" != "null" ]] || { log "skip: entry $idx has no username"; continue; }

    if [[ "$DRY_RUN" -eq 0 ]] && user_exists "$token" "$username"; then
      log "exists, skipping: $username"
      continue
    fi
    create_user "$token" "$uj"
    [[ "$DRY_RUN" -eq 0 ]] && assign_role "$token" "$username" "$(jq -r '.role // ""' <<<"$uj")"
    if [[ "$WRITE_BACK" -eq 1 && "$DRY_RUN" -eq 0 ]]; then
      write_back "$uj"
      written=$((written + 1))
    fi
    created=$((created + 1))
    log "created: $username${ROLE:+ (role=$ROLE)}"
  done

  log "done. created ${created} new user(s)."
  [[ "$written" -gt 0 ]] && log "credentials written to encrypted store: ${STORE}"
  return 0
}

main "$@"
