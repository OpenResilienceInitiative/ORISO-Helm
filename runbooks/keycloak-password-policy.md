# Keycloak password policy: minimum 16 characters

ORISO-Admin#1272. One rule on every path: 16 characters. The UserService checks the same number
(ORISO-UserService#1528). Operator activation on an existing realm needs Hassan9215's sign-off.

`realm.json` is imported only when the realm is absent. It now carries `"passwordPolicy": "length(16)"`,
so a freshly created realm (for example after the planned database wipe) starts with the rule. An already
running realm keeps its old policy until the steps below are run.

## What the policy does

- It is checked when a password is **set or changed** (reset, admin-set, account console, user creation with a password).
- It does **not** log anyone out or invalidate existing passwords; users with an older, shorter password can still sign in
  until they change it.
- Users created without a password (invites, break-glass user, `credentials: []` in `realm.json`) are not affected at import.

## Order

1. Deploy ORISO-UserService#1528 first (server rejects < 16 with a clear message).
2. Re-seed test and E2E accounts with 16+ characters (`scripts/seed-keycloak-users.sh` already generates 24).
3. Then apply the realm policy. Applying it first makes Keycloak answer with its own, less friendly error on paths the UserService has not yet been updated for.

## Apply on an existing realm

```sh
kubectl -n <ns> exec -it deploy/keycloak -- bash -ec '
  export KC_CLI_PASSWORD="$KEYCLOAK_ADMIN_PASSWORD"
  KC=/opt/keycloak/bin/kcadm.sh
  $KC update realms/online-beratung --no-config --server http://localhost:8080/auth --realm master --user "$KEYCLOAK_ADMIN" \
    -s "passwordPolicy=length(16)"
  $KC get realms/online-beratung --no-config --server http://localhost:8080/auth --realm master --user "$KEYCLOAK_ADMIN" \
    --fields passwordPolicy'
```

Expected read-back: `"passwordPolicy" : "length(16)"`.

## Recovery

Set the previous value back with the same command (`-s "passwordPolicy="` clears it). Existing sessions and passwords are untouched either way.
