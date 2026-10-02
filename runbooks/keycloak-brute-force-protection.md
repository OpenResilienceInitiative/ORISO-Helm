# Keycloak brute-force protection: rollout on an existing realm

ORISO-UserService#1338, slice 5. Needs Hassan9215's sign-off before any step runs.

`realm.json` is imported only when the realm does not exist yet
(`kc.sh start --import-realm`). On Dev, Stage and production the realm already
exists, so merging the chart changes **nothing** in the realm. The realm values
have to be set once by an operator, after the Keycloak pod runs with the
concurrency setting from this chart.

## Values

| Realm field | Value | Why |
|---|---|---|
| `bruteForceProtected` | `true` | Switch it on. |
| `permanentLockout` / `maxTemporaryLockouts` | `false` / `0` | Never disable an account for good; every lock ends by itself. |
| `failureFactor` | `15` | A legitimate e-mail sign-in counts up to 9 failures (1 code challenge + 5 resends + 3 wrong codes); 15 leaves room for password typos. |
| `bruteForceStrategy` / `waitIncrementSeconds` / `maxFailureWaitSeconds` | `MULTIPLE` / `60` / `900` | From the 15th failure on, each further failure locks for 60 s × ⌊failures / 15⌋, at most 15 minutes. |
| `maxDeltaTimeSeconds` | `43200` | The count is forgotten after 12 h without a failure; a successful sign-in clears it at once. |
| `quickLoginCheckMilliSeconds` / `minimumQuickLoginWaitSeconds` | `1000` / `5` | Two failures within 1 s (a double click) cost 5 s, not Keycloak's default 60 s. |

Chart value `online-counseling-keycloak.bruteForce.allowConcurrentRequests`
(default `true`) sets
`KC_SPI_BRUTE_FORCE_PROTECTOR__DEFAULT_BRUTE_FORCE_DETECTOR__ALLOW_CONCURRENT_REQUESTS`.
Without it, Keycloak refuses a second login of the same user while the first is
still running. All backends sign in as the shared `technical` user, often in
parallel.

## Steps

1. Deploy the chart that contains this runbook. Confirm the setting in the
   running pod:
   `kubectl -n <ns> exec deploy/keycloak -- /opt/keycloak/bin/kc.sh show-config | grep allow-concurrent-requests`
   → `... = true`.
2. Check that no backend is failing to sign in right now (UserService and
   ConsultingTypeService logs: no 401 from the token endpoint). A wrong
   `technical` password would lock that user for every backend as soon as
   protection is on.
3. Set the realm values from inside the Keycloak pod (the password stays out
   of process arguments):

   ```sh
   kubectl -n <ns> exec -it deploy/keycloak -- bash -c '
     export KC_CLI_PASSWORD="$KEYCLOAK_ADMIN_PASSWORD"
     KC=/opt/keycloak/bin/kcadm.sh
     $KC config credentials --server http://localhost:8080/auth --realm master --user "$KEYCLOAK_ADMIN" --config /tmp/kcadm.config
     $KC update realms/online-beratung --config /tmp/kcadm.config \
       -s bruteForceProtected=true -s permanentLockout=false -s maxTemporaryLockouts=0 \
       -s bruteForceStrategy=MULTIPLE -s failureFactor=15 -s waitIncrementSeconds=60 \
       -s maxFailureWaitSeconds=900 -s maxDeltaTimeSeconds=43200 \
       -s quickLoginCheckMilliSeconds=1000 -s minimumQuickLoginWaitSeconds=5
     $KC get realms/online-beratung --config /tmp/kcadm.config \
       --fields bruteForceProtected,permanentLockout,maxTemporaryLockouts,bruteForceStrategy,failureFactor,waitIncrementSeconds,maxFailureWaitSeconds,maxDeltaTimeSeconds,quickLoginCheckMilliSeconds,minimumQuickLoginWaitSeconds
     rm -f /tmp/kcadm.config'
   ```

4. Smoke test: sign in to the app and the Admin panel with e-mail 2FA, and
   through the Matrix/Element SSO page. Check that backend features that need
   the `technical` user still work (for example creating a chat).

## If something goes wrong

- Switch it off again (takes effect at once, no restart):
  `kcadm.sh update realms/online-beratung -s bruteForceProtected=false`.
- Unlock one user: Admin console → Users → the user → "Temporarily locked" off,
  or `DELETE /admin/realms/online-beratung/attack-detection/brute-force/users/<user-id>`.
- Unlock everyone: `DELETE /admin/realms/online-beratung/attack-detection/brute-force/users`.

## Known risk: deliberate lockout of a known username

Anyone who knows a username can send wrong passwords and keep that account
locked (up to 15 minutes per failure once 15 failures are reached). That includes
`technical` and `svc-keycloak-admin`, whose names are public in `realm.json`.
Service-account tokens (`client_credentials`) are not subject to brute-force
protection; moving the backends to them removes this risk and should be a separate
follow-up; it is not part of this change. The per-IP limit on the token endpoint
(`global.keycloak.tokenRateLimit`, slice 4 of #1338) slows such attempts but does not prevent them.
