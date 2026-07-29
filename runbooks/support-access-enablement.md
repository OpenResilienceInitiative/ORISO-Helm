# Runbook: enabling Global Support Access (ADR-018)

Support access lets a Global Support Admin (GSA) help one consultant in a fresh encrypted 1:1 Matrix
room, created only after both people confirm a live handshake, and withdrawn after four hours at the
latest. It ships disabled in every environment.

## Before enabling

1. **Create the realm role.** `realm.json` is imported on first start only, so an already-running
   Keycloak does not have `global-support-admin`. Create it once:

   ```
   kubectl -n <ns> exec deploy/keycloak -- bash -s \
     < ORISO-Keycloak/scripts/keycloak-apply-support-admin-role.sh
   ```

   The script is idempotent and prints the role back. Without it, creating a GSA fails at role
   assignment and the account stays `PROVISIONING_FAILED` — unusable, but visible in the Admin board.

2. **Check the migrations applied.** Changesets `0073`–`0076` add the widened `admin.type`, the
   support-admin profile, the handshake tables and `support_access_session`.

3. **Confirm the second factor works** in this environment. A GSA cannot start anything until
   Keycloak reports an enrolled OTP credential, and a Keycloak lookup failure fails closed.

## Enabling

```yaml
userService:
  supportAccess:
    enabled: "true"
```

Roll out UserService. Nothing else changes: the flag gates only new handshakes.

## Alerts to have in place first

| Metric | Meaning | Suggested alert |
| --- | --- | --- |
| `oriso.support_access.sessions.revocation_pending_overdue` | Access was withdrawn logically but Matrix has not confirmed it | `> 0` for 2 minutes — page |
| `oriso.support_access.sessions.expired_unverified` | Lease is over while the session is not terminal | `> 0` for 5 minutes — page |
| `oriso.support_access.jobs.failed` | A provisioning job gave up | `> 0` — ticket |
| `oriso.support_access.jobs.oldest_pending_age_seconds` | Worker is stuck or the homeserver is down | `> 600` — ticket |
| `oriso.support_access.sessions.provisioning_failed` | A session could not be built | `> 0` — ticket |

The first two are the ones that matter: both mean a support identity may still be able to reach a
room it should have lost. Withdrawal jobs retry forever by design and never end up `FAILED`, so the
absence of failures is not evidence that withdrawal succeeded — `revocation_pending_overdue` is.

## Rollback

Set `supportAccess.enabled: "false"` and roll out, then disable the test GSA accounts in the Admin
board. Disabling an account immediately blocks new handshakes and marks its running sessions for
revocation.

Do **not** roll back the database migrations. The revocation worker, the four-hour expiry sweep and
the audit retention job keep running while the flag is off — that is deliberate, so switching the
feature off can never strand a session that is still granting access.

## What is intentionally not automated

Enabling on an environment is a manual, per-environment decision. There is no Pre-Dev values file in
this repository, and per the operator decision on PR #157 the current deployment process stays as
it is, so the Pre-Dev override lives in the deployment values on the server rather than here.
