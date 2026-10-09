# Keycloak break-glass access and credential rotation

ORISO-Helm#422. The chart used to give two different Keycloak accounts the same name, `realmadmin`:

- the **master bootstrap admin** in the `master` realm, created by Keycloak itself on first start from
  `global.secrets.keycloakAdminUsername` / `keycloakAdminPassword` (Secret `keycloak-secret-env`,
  keys `KEYCLOAK_ADMIN` / `KEYCLOAK_ADMIN_PASSWORD`);
- the **ORISO platform admin** `realmadmin` in the ORISO realm (`online-beratung`), shipped in `realm.json`
  and, on fresh installs, given the master password plus the `technical` role by the bootstrap Job.

They are two separate user objects that shared a name and, on fresh installs, a password.

Now the master bootstrap admin is the only break-glass identity. Its default name is `oriso-bootstrap-admin`. The
ORISO-realm `realmadmin` ships disabled with no password, and the bootstrap Job keeps it that way. No automation logs
in as it. Hook Jobs log in only to `master`, as the bootstrap admin. Runtime services and the SMTP sync use realm
clients (`backend-technical`, `backend-admin`, `smtp-sync`).

## Who may use it

Only platform operators who already hold cluster-admin on the target cluster. Today on Dev, that means the people
who can run the Dev deploy scripts. Production needs a named list that the platform owner keeps. Every use is
announced in the team's operations channel before it starts and again when it ends, with the reason. A use
that nobody announced is treated as an incident.

## Where the credential lives

- Live: Secret `keycloak-secret-env` in the release namespace, keys `KEYCLOAK_ADMIN` and `KEYCLOAK_ADMIN_PASSWORD`.
- Source: the environment's private persistent secrets values, `global.secrets.keycloakAdminUsername` and
  `keycloakAdminPassword`. It is never stored in this repository, an issue, Slack or a log.
- Keycloak reads these values **only on its first start**, when `master` has no users. After that, changing the Secret
  does not change the live account; see "Rotation" below.

## How to use it

Use it from inside the cluster with `kcadm`. The credential stays in the pod environment and is never printed. Prefer a
short-lived personal admin over enabling `realmadmin`.

**For operators — open an admin session in the Keycloak pod (skip if you only need the procedure):**
```text
NS=caritas   # release namespace
kubectl -n "$NS" exec -it deploy/keycloak -- /bin/bash
# inside the pod; the variables come from keycloak-secret-env
KC=/opt/keycloak/bin/kcadm.sh
$KC config credentials --server http://localhost:8080/auth --realm master \
  --user "$KEYCLOAK_ADMIN" --password "$KEYCLOAK_ADMIN_PASSWORD" --config /tmp/kcadm.config
alias kc="$KC --config /tmp/kcadm.config"
# ... work ...
rm -f /tmp/kcadm.config; exit
```

**For operators — temporary ORISO platform admin for the Admin UI (preferred: a named person, removed afterwards):**
```text
REALM=online-beratung
kc create users -r "$REALM" -s username=breakglass-<name>-<yyyymmdd> -s enabled=true \
  -s email=<name>@<org-domain> -s emailVerified=true -s 'attributes.tenantId=["0"]'
kc set-password -r "$REALM" --username breakglass-<name>-<yyyymmdd> --new-password '<generated>' --temporary
for r in user-admin tenant-admin single-tenant-admin agency-admin topic-admin restricted-agency-admin; do
  kc add-roles -r "$REALM" --uusername breakglass-<name>-<yyyymmdd> --rolename "$r"; done
# never grant the realm role "technical" to a person
# when done:
kc delete users/<id> -r "$REALM"
```

**For operators — fallback: enable the shipped `realmadmin` for a session, then disable it again:**
```text
UID=$(kc get users -r "$REALM" -q exact=true -q username=realmadmin --fields id --format csv --noquotes)
kc update users/$UID -r "$REALM" -s enabled=true
kc set-password -r "$REALM" --username realmadmin --new-password '<generated>' --temporary
# when done (always):
kc update users/$UID -r "$REALM" -s enabled=false
kc create users/$UID/logout -r "$REALM"
```
If `global.keycloak.bootstrapUsers.realmAdmin.disableExisting` is true, the next install or upgrade disables it again
anyway.

## How a use is noticed

The chart does not configure event storage for `master`. The ORISO realm keeps `eventsEnabled` and
`adminEventsEnabled` off. Until a chart change covers this, an operator turns them on once per environment, from the
session above:

```text
kc update realms/master -s eventsEnabled=true -s adminEventsEnabled=true -s adminEventsDetailsEnabled=true \
  -s eventsExpiration=7776000
kc update realms/online-beratung -s adminEventsEnabled=true -s eventsExpiration=7776000
# read back
kc get events -r master --limit 50          # LOGIN / LOGIN_ERROR of the bootstrap admin
kc get admin-events -r online-beratung --limit 50
```

The `jboss-logging` listener already writes failed logins to the Keycloak pod log at WARN, with `realmName="master"`.
It writes successful logins only at DEBUG. To get every master login into the log shipping, raise that listener's
success level to `info` (a Keycloak server option; the exact spelling depends on the running Keycloak version). The
chart's own hook Jobs also log in to `master` on every install or upgrade. A master login outside a deploy window, or
without an announcement, is an incident.

## Disabling the ORISO-realm `realmadmin` on an existing realm

Fresh imports already ship it disabled. Existing realms keep whatever state they have until an operator opts in:

1. Confirm the master break-glass session above works in that environment.
2. Confirm that nobody still signs in as `realmadmin`. In the Admin Console, open the user's sessions, read the
   last-login events if events are on, and ask the team.
3. Set `global.keycloak.bootstrapUsers.realmAdmin.disableExisting: true` in the environment values and upgrade. The
   `keycloak-reconcile-service-identities` hook disables the user and ends its sessions after the client reconcile
   has succeeded. It prints `REALM_ADMIN_DISABLED`, or `REALM_ADMIN_ABSENT` if the user does not exist. Repeat runs
   change nothing.
4. Rollback: set the flag back to false, then re-enable the user via break-glass if it is really needed.

## Renaming the master bootstrap admin on an existing install

Keycloak ignores a changed `keycloakAdminUsername` once `master` has users. Every hook Job would then fail to log
in. A fresh install refuses to render when the master name equals `bootstrapUsers.realmAdmin.username`. An upgrade
still renders the old shared name, so existing installs keep working until this migration is done:

1. In a break-glass session, create the new master user. It has the realm role `admin` in `master` and a fresh
   generated password: `kc create users -r master -s username=oriso-bootstrap-admin -s enabled=true`, then
   `kc set-password ...` and `kc add-roles -r master --uusername oriso-bootstrap-admin --rolename admin`.
2. Log in once as the new user with `kcadm config credentials --realm master` to prove it works.
3. Put the new name and password into the private secrets values, then upgrade. The service-identity hook logs in
   with them, so a successful hook proves the switch.
4. Disable the old **master-realm** `realmadmin` (`-r master`, not the ORISO realm). Delete it after one quiet
   release.

## Rotation

Rotate only after Helm#420 is deployed in that environment. Before Helm#420, the SMTP sync ran with the master
password and the `smtp-sync` client did not exist. Rotate one credential at a time and verify each before starting
the next.

| Order | Credential | Where | How it reaches Keycloak | Verify |
|---|---|---|---|---|
| 1 | `smtp-sync` client secret | `global.secrets.keycloakSmtpSyncClientSecret` | service-identity hook sets it on upgrade; the next SMTP Job uses the new Secret | hook prints `SMTP_SYNC_CLIENT_RECONCILED`; next SMTP Job applies and acks |
| 2 | `backend-technical` client secret | `global.secrets.keycloakBackendTechnicalClientSecret` | same hook; backend Pods roll (checksum) | hook prints `BACKEND_CLIENTS_RECONCILED`; invite + mail flow on the environment |
| 3 | `backend-admin` client secret | `global.secrets.keycloakBackendAdminClientSecret` | same hook; backend Pods roll | same; user management in Admin |
| 4 | legacy `svc-keycloak-admin` password user | no chart consumer left | retire, do not rotate: `backendServiceClients.retireLegacyUsers: true` once 2-3 are verified | user disabled, sessions ended |
| 5 | master bootstrap admin password | `global.secrets.keycloakAdminPassword` | **not** applied by Keycloak; set it live first, then upgrade | hooks log in successfully |

**For operators — client secret rotation (rows 1-3):**
```text
1. Generate a new value of at least 32 printable characters, independent of the other two.
2. Put it into the private secrets values; upgrade the release.
3. The keycloak-reconcile-service-identities hook writes the new secret and issues a real token with it before it
   succeeds. Backend Pods restart on the checksum change. Expect a few minutes in which old Pods are refused.
   Do it outside working hours.
4. If the hook fails, nothing was retired. Restore the previous value and upgrade again.
```

**For operators — master password rotation (row 5):**
```text
1. Break-glass session with the CURRENT password (see above).
2. kc set-password -r master --username "$KEYCLOAK_ADMIN" --new-password '<new>'
3. Immediately put <new> into global.secrets.keycloakAdminPassword and upgrade. Between steps 2 and 3, only the
   install/upgrade hooks use it. Since Helm#420 no CronJob holds it.
4. The hooks (service identities, 2FA check, mail locales) log in with the new password; their success is the proof.
5. If step 3 fails, set the old password back live with step 2 and investigate.
```
