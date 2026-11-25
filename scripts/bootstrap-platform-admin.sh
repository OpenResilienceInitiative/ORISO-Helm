#!/usr/bin/env bash
# Creates the first ORISO platform admin on a freshly installed environment.
#
# Needed because no supported route exists on a new install:
#   * realm.json already contains `realmadmin`, so keycloak-bootstrap-users takes its
#     "exists and disableExisting is false" early exit and grants it nothing; and it is
#     shaped disabled with no password by design (ORISO-Helm#422).
#   * A hand-made Keycloak user is refused by AccountInactivityAccessFilter: admit() is
#     fail-closed without an `account_inactivity` row, and that row is only seeded from
#     user/consultant/admin rows, which can only be created from inside the Admin UI.
#
# Run it, create the real admins through the Admin UI, then --remove this one.
set -euo pipefail

NS=${NS:-caritas}
REALM=${REALM:-online-beratung}
DOMAIN=${DOMAIN:-dev.oriso.org}

usage() {
  cat <<EOF
Usage: $(basename "$0") <username> <email> [password]
       $(basename "$0") --remove <username>

  NS=$NS  REALM=$REALM  DOMAIN=$DOMAIN   (override with env vars)

Without a password one is generated and printed once.
EOF
}

# The eleven realm roles the chart grants realmadmin. `consultant` is explicitly removed:
# UserService routes /users/data down the consultant branch when it is present, and a
# platform admin has no consultant row, so the request 500s.
ROLES=(default-roles-online-beratung offline_access uma_authorization user
       user-admin USER_ADMIN agency-admin topic-admin single-tenant-admin
       restricted-agency-admin tenant-admin)

case "${1:-}" in -h|--help|"") usage; exit 0 ;; esac

MODE=create
if [ "$1" = "--remove" ]; then MODE=remove; USERNAME=${2:?username required}; else
  USERNAME=$1; EMAIL=${2:?email required}; PASSWORD=${3:-}
  if [ -z "$PASSWORD" ]; then
    # Realm policy is length(16); 24 chars from /dev/urandom keeps it comfortably over.
    PASSWORD=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)
    GENERATED=yes
  fi
fi

need() { command -v "$1" >/dev/null || { echo "missing required command: $1" >&2; exit 1; }; }
need kubectl

kc_pod()      { kubectl -n "$NS" get pods -l app=keycloak -o name 2>/dev/null | head -1; }
maria_exec()  { kubectl -n "$NS" exec -i mariadb-0 -- sh -c 'mariadb -uroot -p"${MYSQL_ROOT_PASSWORD:-$MARIADB_ROOT_PASSWORD}" userservice'; }

# --- Keycloak side -----------------------------------------------------------------
# Runs inside the keycloak pod so KEYCLOAK_ADMIN/_PASSWORD never leave it. Prints the
# user id on the last line.
keycloak_step() {
  kubectl -n "$NS" exec -i deploy/keycloak -- bash -s -- "$REALM" "$USERNAME" "${EMAIL:-}" "${PASSWORD:-}" "$MODE" "${ROLES[@]}" <<'INNER'
set -euo pipefail
REALM=$1; U=$2; EMAIL=$3; PW=$4; MODE=$5; shift 5; ROLES=("$@")
KC=/opt/keycloak/bin/kcadm.sh
CFG=/tmp/kcadm.$$.config
cleanup() { rm -f "$CFG" /tmp/bootstrap-admin.$$.json; }
trap cleanup EXIT
"$KC" config credentials --server http://localhost:8080/auth --realm master \
  --user "$KEYCLOAK_ADMIN" --password "$KEYCLOAK_ADMIN_PASSWORD" --config "$CFG" >/dev/null
kc() { "$KC" "$@" --config "$CFG"; }

uid_of() { kc get users -r "$REALM" -q exact=true -q username="$U" --fields id --format csv --noquotes 2>/dev/null | tr -d '\r' | head -1; }

if [ "$MODE" = remove ]; then
  ID=$(uid_of); [ -n "$ID" ] || { echo "not found: $U" >&2; exit 1; }
  kc delete "users/$ID" -r "$REALM"
  echo "$ID"; exit 0
fi

ID=$(uid_of)
if [ -z "$ID" ]; then
  kc create users -r "$REALM" -s username="$U" -s enabled=true -s email="$EMAIL" -s emailVerified=true >/dev/null
  ID=$(uid_of)
fi
[ -n "$ID" ] || { echo "could not resolve the user id" >&2; exit 1; }

# Attributes must be written from a JSON file. `kcadm -s attributes.x=[...]` does not
# serialise reliably here, and `kcadm get --fields attributes` renders nested maps as {}
# so it cannot be used to verify — read the full representation instead.
cat >/tmp/bootstrap-admin.$$.json <<JSON
{ "username": "$U", "enabled": true, "emailVerified": true,
  "attributes": { "tenantId": ["0"], "userId": ["$ID"], "userName": ["$U"] } }
JSON
kc update "users/$ID" -r "$REALM" -f /tmp/bootstrap-admin.$$.json
kc set-password -r "$REALM" --username "$U" --new-password "$PW"
for r in "${ROLES[@]}"; do kc add-roles -r "$REALM" --uusername "$U" --rolename "$r" >/dev/null 2>&1 || true; done
kc remove-roles -r "$REALM" --uusername "$U" --rolename consultant >/dev/null 2>&1 || true
echo "$ID"
INNER
}

if [ "$MODE" = remove ]; then
  ID=$(keycloak_step | tail -1)
  printf "DELETE FROM account_inactivity WHERE identity_id='%s';\n" "$ID" | maria_exec
  echo "removed $USERNAME ($ID) from Keycloak and account_inactivity"
  exit 0
fi

ID=$(keycloak_step | tail -1)
echo "keycloak user: $USERNAME ($ID)"

# --- UserService side --------------------------------------------------------------
# Mirrors the row 20260916_account_inactivity would have seeded from an admin row.
cat <<SQL | maria_exec
INSERT IGNORE INTO account_inactivity
 (identity_id, tenant_id, assigned_months, revision, last_activity, due_at, status)
VALUES
 ('$ID', 0, 24, 0, UTC_TIMESTAMP(6), DATE_ADD(UTC_TIMESTAMP(6), INTERVAL 24 MONTH), 'ACTIVE');
SELECT identity_id, tenant_id, status FROM account_inactivity WHERE identity_id='$ID';
SQL

# --- verify ------------------------------------------------------------------------
TOK=$(curl -s --data-urlencode 'client_id=app' --data-urlencode "username=$USERNAME" \
      --data-urlencode "password=$PASSWORD" --data-urlencode 'grant_type=password' \
      "https://$DOMAIN/auth/realms/$REALM/protocol/openid-connect/token" \
      | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))')
if [ -z "$TOK" ]; then echo "WARN: could not obtain a token; check the password policy" >&2; else
  python3 - "$TOK" <<'PY'
import sys, json, base64
p = sys.argv[1].split('.')[1]; p += '=' * (-len(p) % 4)
d = json.loads(base64.urlsafe_b64decode(p))
roles = sorted(d.get('realm_access', {}).get('roles', []))
print("tenantId claim :", d.get('tenantId'))
print("userId claim   :", d.get('userId'))
print("consultant role:", 'consultant' in roles, "(must be False)")
print("roles          :", ', '.join(roles))
PY
  printf 'users/data     : %s (want 200)\n' \
    "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOK" "https://$DOMAIN/service/users/data")"
fi

echo
echo "Admin UI: https://$DOMAIN/admin"
echo "username: $USERNAME"
[ "${GENERATED:-}" = yes ] && echo "password: $PASSWORD   (shown once)"
echo
echo "Create the real platform admins through the Admin UI, then remove this one:"
echo "  $(basename "$0") --remove $USERNAME"
