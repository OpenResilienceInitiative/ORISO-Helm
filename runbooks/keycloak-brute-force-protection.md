# Keycloak brute-force protection: existing realm activation

ORISO-UserService#1338, slice 5. Operator activation needs Hassan9215's sign-off. Source changes and isolated tests do not activate any shared realm.

Realm import creates a missing realm; it does not update existing Dev, Stage or production realms. Complete every prerequisite below before enabling protection on an existing realm.

## Required dependencies

1. Deploy and verify Keycloak#50: requesting or waiting for an email/app OTP, the mail cap and SMTP failure must not consume credential failures. Wrong passwords and submitted wrong codes must still count.
2. Complete UserService#1351: backends use separately scoped confidential service clients with client_credentials and no password fallback. Verify chat, invitations, OTP administration and SMTP reconciliation with strict subject/client checks retained.
3. Retire the old technical and svc-keycloak-admin password-login paths. A deliberate lock of a disposable old password account must not interrupt backend features. Check every caller; clean recent logs alone do not prove migration.
4. Verify a working master-realm recovery admin outside application-realm protection. Rehearse recovery only in an isolated fixture.

Do not accept shared technical-account lockout as a default risk. The optional ingress rate limit slows requests but cannot prevent deliberate lockout of a known password account.

## Values

| Realm field | Value | Behavior |
|---|---|---|
| bruteForceProtected | true | Enables credential-guess protection. |
| permanentLockout / maxTemporaryLockouts | false / 0 | No permanent lock through these settings. |
| maxSecondaryAuthFailures | 0 | Disables the separate permanent OTP lockout path, which can disable accounts even when permanentLockout is false. |
| failureFactor | 15 | Fifteen actual credential failures trigger the first regular wait. Legitimate OTP prompts do not count after Keycloak#50. |
| bruteForceStrategy / waitIncrementSeconds / maxFailureWaitSeconds | MULTIPLE / 60 / 900 | 60 × floor(count / 15), capped at 900 seconds: counts 15–29 wait 60 seconds, 30–44 wait 120 seconds. Refusals during a lock do not increase the count. |
| maxDeltaTimeSeconds | 43200 | The next failure after more than 12 hours without a failure starts a new count. Successful authentication clears it. |
| quickLoginCheckMilliSeconds / minimumQuickLoginWaitSeconds | 1000 / 5 | Closely spaced actual failures can trigger a five-second wait before the regular threshold. |

Keep chart value online-counseling-keycloak.bruteForce.allowConcurrentRequests=false. Client-credentials grants avoid same-user password-login contention. Globally allowing parallel human authentication produced one success and one HTTP 500 for simultaneous use of one email OTP in the isolated H2 runtime; the default guard produced one success and a clean HTTP 400 refusal. This observation does not establish MariaDB behavior.

The former “one prompt plus five resends plus three wrong codes equals nine” rationale was incorrect: a mail cap does not cap repeated token requests, cooldown prompts or abandoned challenges.

## Operator steps

1. Verify deployed revisions for both dependencies and actual service-client token claims. Confirm the pod configuration reports allow-concurrent-requests=false:

   ```sh
   kubectl -n <ns> exec deploy/keycloak -- /opt/keycloak/bin/kc.sh show-config | grep allow-concurrent-requests
   ```

2. Apply and read back all realm fields. The password stays out of arguments and --no-config avoids writing an admin-token configuration file:

   ```sh
   kubectl -n <ns> exec -it deploy/keycloak -- bash -ec '
     export KC_CLI_PASSWORD="$KEYCLOAK_ADMIN_PASSWORD"
     KC=/opt/keycloak/bin/kcadm.sh
     $KC update realms/online-beratung --no-config --server http://localhost:8080/auth --realm master --user "$KEYCLOAK_ADMIN" \
       -s bruteForceProtected=true -s permanentLockout=false -s maxTemporaryLockouts=0 -s maxSecondaryAuthFailures=0 \
       -s bruteForceStrategy=MULTIPLE -s failureFactor=15 -s waitIncrementSeconds=60 \
       -s maxFailureWaitSeconds=900 -s maxDeltaTimeSeconds=43200 \
       -s quickLoginCheckMilliSeconds=1000 -s minimumQuickLoginWaitSeconds=5
     $KC get realms/online-beratung --no-config --server http://localhost:8080/auth --realm master --user "$KEYCLOAK_ADMIN" \
       --fields bruteForceProtected,permanentLockout,maxTemporaryLockouts,maxSecondaryAuthFailures,bruteForceStrategy,failureFactor,waitIncrementSeconds,maxFailureWaitSeconds,maxDeltaTimeSeconds,quickLoginCheckMilliSeconds,minimumQuickLoginWaitSeconds'
   ```

3. Verify app/Admin/Matrix SSO sign-in, email resend, abandoned prompts, wrong-code refusal and correct-code completion. Verify chat, invitations and SMTP while a disposable legacy password account is locked. Never conduct failed-password experiments on a shared live technical account.

## Recovery

Use the verified master-realm administrator with the same --no-config connection options. Disable application-realm protection with update realms/online-beratung -s bruteForceProtected=false if necessary. Unlock a user through DELETE /admin/realms/online-beratung/attack-detection/brute-force/users/<user-id>, or all users through DELETE /admin/realms/online-beratung/attack-detection/brute-force/users. Keep recovery access available before activation.

## Evidence boundary

Audit baseline used the exact Dev image (Keycloak 26.6.3, revision 04dc125) in isolated synthetic H2 fixtures. MULTIPLE boundaries were measured with accelerated waits; the 12-hour and 15-minute limits were checked against matching upstream source rather than waited in full. Isolated source/runtime tests are separate from deployed MariaDB, ingress, actual-browser and shared-realm acceptance. No live realm was changed by the audit.
