# Real five-actor receiver matrix

`scenario_c_receiver_fixture.py` prepares exactly five synthetic actor clients in a disposable, digest-pinned Keycloak 26.6.3 container. It supplies `identityModel: FIVE_ACTORS`, actual service-account subjects, exact realm-role unions, receiver audiences, and native signed fault tokens to the receiving services' `RealTaskTokenAuthorizationIT`.

It imports no old fourteen-client registry, chart reconciler, account provider or production realm. This proves the selected grouped receiving contract at normal resource-server JWT and HTTP boundaries. It does not prove Helm native-policy import, account creation/recovery, SMTP field restrictions, deployment or Dev acceptance.

## Fixture contract

| Actor | Client | Receiving audiences |
|---|---|---|
| SETUP | backend-setup | tenantservice, agencyservice, consultingtypeservice, userservice |
| ACCOUNT_CREATE | backend-account-create | userservice |
| ACCOUNT_ADMIN | backend-account-admin | userservice, oriso-otp |
| MAIL | backend-mail | tenantservice, agencyservice, consultingtypeservice, userservice, oriso-task-commands |
| RUNTIME | backend-runtime | tenantservice, agencyservice, appointmentservice |

The role unions are the five-actor contract selected in Helm423. Before and after generating faults, the runner checks actual effective realm and client grants and group memberships. There are no baseline-role, native administration or group grants on these synthetic service accounts. Native fine-grained administration permissions are not configured by this fixture.

For each actor it obtains three negative tokens from Keycloak itself: an added unrelated realm grant, an unrelated receiver audience, and an unbound subject. It temporarily changes only the isolated native role/scope or mapper configuration, obtains a signed token, and restores the original configuration in `finally` blocks. It then obtains and checks another valid token. It never builds a locally forged JWT.

## Run

Install the existing Helm test dependencies and provide Docker. Acquire the shared test lease before starting a container or Maven run. Receiver worktrees must contain the grouped native test class and the explicit `native-permission-matrix` Maven profile.

**For developers — fixture-only check, with no Java receiver execution:**

```bash
ORISO_REQUIRE_KEYCLOAK_DOCKER=1 python3 tests/scenario_c_receiver_fixture.py --verify-fixture-only
```

Only the model, actor/variant counts and actor keys are printed. The private fixture, synthetic tokens, raw native logs and owned container are removed when the context exits.

**For developers — run the two actual receiver suites sequentially; supply local worktree paths and the supported JDK:**

```bash
ORISO_REQUIRE_KEYCLOAK_DOCKER=1 python3 tests/scenario_c_receiver_fixture.py \
  --receiver /local/worktrees/ORISO-TenantService \
  --receiver /local/worktrees/ORISO-AgencyService \
  --java-home /local/jdk-home \
  --expected-tests 5
```

The runner uses `ORISO_TASK_TOKEN_FIXTURE` and invokes Maven with `-Pnative-permission-matrix -Dit.test=RealTaskTokenAuthorizationIT verify`. It requires a newly written, single-suite Failsafe XML for each receiver, exactly five cases and zero failures, errors or skips. Surefire-only output, old reports and an absent profile cannot satisfy the gate. Extend the expected case count explicitly when extending the suite; do not lower it to hide missing cases.

Receipts contain counts, Git revision, test-source SHA256 and POM SHA256, never tokens or synthetic secrets. Source hashes identify the actual files when a worktree contains an uncommitted change. A receipt is native source-test evidence, not production authorization or a merge/deployment claim.

**For developers — independently verify a report using a timestamp captured before its native run:**

```bash
python3 tests/scenario_c_receiver_fixture.py \
  --verify-report /local/worktrees/ORISO-TenantService \
  --started START_TIMESTAMP \
  --expected-tests 5
python3 tests/test_scenario_c_receiver_fixture.py -v
```

`START_TIMESTAMP` is the positive Unix timestamp from before Maven starts. The CLI rejects invalid/nonfinite timestamps, missing/duplicate/stale reports, malformed XML, skipped cases and zero tests. Supplying this timestamp checks a report; it does not independently establish that the caller executed Maven or used this fixture.

For another native receiver, use the public `fixture_file()` context manager from the same tests directory, preserve its temporary lifetime, pass the fixture environment to the bounded suite, and retain the fresh execution-report gate. Never copy the fixture contents into a PR, artifact directory or repository.
