# Runbook: Liquibase Baseline Sync of Existing Environments (Package L2)

**Status:** ready for execution — must be completed per environment BEFORE package L3
(ORISO-Helm flips `SPRING_LIQUIBASE_ENABLED` back to `true`) rolls out there.

**Audience:** operator with database access to the target environment and Docker on
the workstation.

**Scope:** the four Liquibase-managed service databases:
`tenantservice`, `userservice`, `agencyservice`, `consultingtypeservice`.

**Why this lives in ORISO-Helm:** the original home, ORISO-Database, was archived
on 2026-06-30; ORISO-Helm is the canonical infrastructure repo since then, and
package L3 (the `SPRING_LIQUIBASE_ENABLED` default flip) lands here as well.
The schema dumps referenced below remain readable in the archived
[ORISO-Database](https://github.com/OpenResilienceInitiative/ORISO-Database) repo.

---

## 1. Why this is needed

- Liquibase has been disabled in all four backends since Feb 2026
  (`liquibase.enabled=false`, later enforced by cluster configmaps setting
  `SPRING_LIQUIBASE_ENABLED=false`). All schema changes since then were applied by
  hand (`ALTER TABLE` directly on the database).
- Package L1 (merged) gave every service a single consolidated master changelog
  (`db/changelog/<service>-master.xml`) and a kill switch
  (`spring.liquibase.enabled=${SPRING_LIQUIBASE_ENABLED:true}`).
- The `DATABASECHANGELOG` tables in existing environments are therefore **stale**:
  they end at (roughly) the Feb 2026 state, while the actual schema is months ahead.
- Historical changesets are largely **non-idempotent**. If Liquibase were simply
  switched on, the first `update` would try to re-run changesets whose effects
  already exist — failing on the first duplicate `CREATE TABLE`, or worse,
  **re-running data migrations**.

The fix is a one-time **baseline**: tell Liquibase which changesets are already
effectively applied (`changelog-sync`), then prove that `update` is a no-op.
After that, L3 can flip the default safely: the L1 catch-up changesets carry
`onFail=MARK_RAN` preconditions, so even partially-baselined databases self-heal
instead of failing.

## 2. Tooling and invocation mechanism

One mechanism is used everywhere: the **official Liquibase Docker image, pinned to
`liquibase/liquibase:4.23.2`**, with the service repository's `src/main/resources`
mounted read-only and `LIQUIBASE_SEARCH_PATH=/liquibase/changelog`.

Why this exact setup:

- **4.23.2 matches the `liquibase-core` version** used by TenantService,
  AgencyService and ConsultingTypeService, and produces checksum-format v9,
  which is also what UserService's newer runtime (`liquibase-core` 5.0.1 via
  Spring Boot 4.0.1) reads and writes. Do not use a random newer CLI.
- The search-path arrangement makes Liquibase record `FILENAME` values as
  `db/changelog/changeset/...` — **byte-identical to what the Spring Boot runtime
  records and to the rows already present in every live `DATABASECHANGELOG`**
  (verified on the dev cluster, see Appendix A). Changeset identity is
  `(id, author, filepath)`; a different path would make every changeset look new.
- No local JDK/Maven variance: the image bundles the MariaDB JDBC driver
  (verified against MariaDB 11.8).

The helper script wraps all of this:

```
scripts/liquibase-baseline-sync.sh <service> [--execute] [--mark-ran-count N] [--skip-update] [--fresh]
```

- **Dry-run is the default.** Without `--execute` only the read-only `status`
  step runs; every mutating command is printed instead of executed.
- Credentials are taken **exclusively from environment variables**
  (`LB_DB_USERNAME`, `LB_DB_PASSWORD`, …). Never hardcode credentials in scripts,
  CI files or this runbook — see [ORISO-Database docs/secret-management.md](https://github.com/OpenResilienceInitiative/ORISO-Database/blob/main/docs/secret-management.md) (repo archived, policy still authoritative).
- The script logs every step with timestamps and aborts on the first error
  (`set -euo pipefail`).

Environment variables:

| Variable | Required | Meaning |
|---|---|---|
| `LB_DB_HOST` | yes | DB host **as reachable from inside the liquibase container** |
| `LB_DB_USERNAME` / `LB_DB_PASSWORD` | yes | DB credentials (from the environment's secret store) |
| `LB_CHANGELOG_DIR` | yes | absolute path to the service repo's `src/main/resources` (checked out at current `origin/dev`) |
| `LB_DB_PORT` | no | default `3306` |
| `LB_DB_NAME` | no | default = service name |
| `LB_LIQUIBASE_IMAGE` | no | default `liquibase/liquibase:4.23.2` — keep the pin |
| `LB_DOCKER_NETWORK` | no | Docker network for the CLI container (`host` on Linux, or a user-defined network shared with a local DB container) |

## 3. The baseline procedure (what the script runs)

Per service database:

1. `status --verbose` — read-only; lists every changeset Liquibase believes is
   pending. **Read this list.** It is the whole decision input for step 3.
2. `clear-checksums` — historical changesets were edited over the years, so the
   stored checksums no longer match the files. Clearing them prevents false
   `validation failed` errors; checksums are re-stored during sync/first run.
3. `changelog-sync` — marks **all** pending changesets as ran without executing
   them. This is correct **only if the schema effect of every pending changeset is
   actually present** in the database (see the decision matrix below).
   Alternative: `--mark-ran-count N` marks only the first N pending changesets as
   ran (a `mark-next-changeset-ran` loop), leaving genuinely-missing changesets
   pending so that step 5 applies them for real.
4. `status --verbose` — expect `is up to date`, or exactly the intentionally-left
   pending changesets from step 3.
5. `update` — the no-op proof (`Database is up to date, no changesets to
   execute`), or the application of the intentionally-pending changesets.

Note on verification: in Liquibase 4.23, both `changelog-sync` and
`mark-next-changeset-ran` record `EXECTYPE=EXECUTED` (not `MARK_RAN`), so do not
try to distinguish synced vs executed rows by `EXECTYPE`. Use the step-4 `status`
output and row counts.

## 4. Decision matrix — which mode per environment

| Environment situation | Mode | Reasoning |
|---|---|---|
| Live environment running **current dev images**, services healthy (dev, Pre-Dev) | full `changelog-sync` (script default) | A healthy running service is the proof that the full schema is present — hand-applied or not. Marking everything ran is correct by construction. |
| **Dump-provisioned or uncertain** environment (fresh server seeded from ORISO-Database `mariadb/<svc>/schema.sql`, or an env whose service version is unclear) | review `status` list, verify schema, then `--mark-ran-count N` + `update` | The ORISO-Database dumps lag the changelog (e.g. the agencyservice dump ends at changeset 0018; 0019–0022 are missing). Blind sync would mark missing changes as ran and silently leave the schema broken. Baseline only the verified-present prefix; `update` applies the rest. |
| **Local / throwaway** environment | drop & recreate + `--fresh` | Team rule: no prod users → prefer recreate over migrate. An empty database plus plain `update` rebuilds everything from the changelog and yields a perfect `DATABASECHANGELOG`. |

How to verify the prefix for the selective mode: walk the `status --verbose` list
top-down and spot-check, per pending changeset, whether its object exists
(`SHOW TABLES LIKE ...`, `SHOW COLUMNS FROM <table> LIKE ...`). The first
changeset whose effect is missing is where the mark-ran prefix ends; N = number of
pending **entries** (changesets, not files — one file can contain several) above it.

**Partially-hand-fixed environments are the expected case, not an exception.**
Schema drift on dev is being hand-fixed on an ongoing basis (was actively
happening on 2026-07-04 while this runbook was written). The procedure is safe to
run on such an environment because it never executes historical changesets —
it only records them as ran — and because the capture in Appendix A must be
**re-taken at execution time** (section 5, step 1) rather than trusted from this
document.

## 5. Execution per environment (dev / Pre-Dev cluster)

### Step 0 — prerequisites

- Docker on your workstation, `kubectl`/SSH access to the target cluster.
- The four service repos checked out at current `origin/dev` (worktrees are fine).
- A maintenance window is **not** strictly required (the baseline only writes to
  `DATABASECHANGELOG*`), but avoid running it while someone else is hand-patching
  the same database. **Coordinate in #oriso-codereview / with whoever is doing
  schema-drift fixes that day.**

### Step 1 — capture current state (read-only)

```bash
# find the MariaDB pod
kubectl get pods -A | grep -i mariadb    # e.g. caritas/oriso-mariadb-0

for db in tenantservice userservice agencyservice consultingtypeservice; do
  kubectl exec oriso-mariadb-0 -n caritas -- sh -c \
    "mysql -uroot -p\"\$MYSQL_ROOT_PASSWORD\" -D $db -e \
     'SELECT COUNT(*) FROM DATABASECHANGELOG;
      SELECT FILENAME, ID, AUTHOR, DATEEXECUTED FROM DATABASECHANGELOG ORDER BY DATEEXECUTED DESC LIMIT 5;
      SELECT LOCKED FROM DATABASECHANGELOGLOCK;'"
done
```

Save the output next to this runbook (or in the execution ticket). Compare with
Appendix A; investigate anything unexpected (e.g. new rows dated today = someone
is mid-fix — stop and coordinate). `LOCKED` must be 0.

### Step 2 — reach the database from your workstation

```bash
kubectl port-forward -n caritas oriso-mariadb-0 3316:3306
```

- macOS (Docker Desktop): `LB_DB_HOST=host.docker.internal LB_DB_PORT=3316`
- Linux: `LB_DOCKER_NETWORK=host LB_DB_HOST=127.0.0.1 LB_DB_PORT=3316`

Credentials come from the cluster's secret (the `MYSQL_ROOT_PASSWORD` env of the
MariaDB pod, or the per-service DB user) — export them into
`LB_DB_USERNAME`/`LB_DB_PASSWORD` in your shell, do not write them to disk.

### Step 3 — dry-run every service first

```bash
export LB_DB_HOST=... LB_DB_PORT=... LB_DB_USERNAME=... LB_DB_PASSWORD=...

LB_CHANGELOG_DIR=~/ORISO/ORISO-TenantService/src/main/resources \
  scripts/liquibase-baseline-sync.sh tenantservice

LB_CHANGELOG_DIR=~/ORISO/ORISO-UserService/src/main/resources \
  scripts/liquibase-baseline-sync.sh userservice

LB_CHANGELOG_DIR=~/ORISO/ORISO-AgencyService/src/main/resources \
  scripts/liquibase-baseline-sync.sh agencyservice

LB_CHANGELOG_DIR=~/ORISO/ORISO-ConsultingTypeService/src/main/resources \
  scripts/liquibase-baseline-sync.sh consultingtypeservice
```

Review each pending list against the decision matrix (section 4).

### Step 4 — execute

For a healthy live environment (default expectation on dev/Pre-Dev):

```bash
LB_CHANGELOG_DIR=~/ORISO/ORISO-TenantService/src/main/resources \
  scripts/liquibase-baseline-sync.sh tenantservice --execute
# ... repeat per service
```

Expected per service: `clear-checksums` ok → `changelog-sync` ok → `status` says
`is up to date` → `update` says `Database is up to date, no changesets to execute`.

### Step 5 — record completion

Note per environment and per database: date, operator, pending-count before,
mode used (full sync / `--mark-ran-count N` / fresh), and the final no-op proof
line. **L3 must not be rolled out to an environment without this record.**

## 6. Local / throwaway environments

Do not baseline — recreate:

```bash
mysql -e "DROP DATABASE tenantservice; CREATE DATABASE tenantservice;"
LB_CHANGELOG_DIR=... scripts/liquibase-baseline-sync.sh tenantservice --execute --fresh
```

`--fresh` skips checksums/sync and runs a plain `update` that rebuilds the schema
from the changelog (validated: consultingtypeservice rebuilds its 6 tables +
`DATABASECHANGELOG*` from 11 changesets).

## 7. Remediation

- **Checksum validation error on first service boot after L3** (possible if a
  changeset file changes between baseline and boot, or across Liquibase versions):
  set `SPRING_LIQUIBASE_ENABLED=false` (the L1 kill switch) to restore service,
  run `clear-checksums` for that database via the script mechanism, re-enable.
  Liquibase re-stores checksums for already-ran changesets without re-executing them.
- **`update` tries to run something unexpected**: abort (the script already
  stopped — it aborts on first error), diagnose with `status --verbose`, and
  either mark the changeset ran (if its effect exists) or let update apply it
  (if not). Nothing destructive has happened: baseline steps only write to
  `DATABASECHANGELOG*`.
- **Stale lock** (`DATABASECHANGELOGLOCK.LOCKED=1` with no run in progress):
  `liquibase release-locks` via the same Docker mechanism.

## 8. Validation evidence (2026-07-04, local Docker gate)

Simulated worst case: fresh MariaDB 11.8 in Docker (`-p 3315:3306`), the four
databases loaded from the ORISO-Database `mariadb/<svc>/schema.sql` dumps — i.e. a
dump-provisioned environment with **empty** `DATABASECHANGELOG`.

**TenantService — full baseline, then no-op proof:**

```
23 changesets have not been applied to root@jdbc:mariadb://…/tenantservice
Liquibase command 'clear-checksums' was executed successfully.
Liquibase command 'changelog-sync' was executed successfully.
root@jdbc:mariadb://…/tenantservice is up to date
Database is up to date, no changesets to execute
Liquibase command 'update' was executed successfully.
```

**UserService — full baseline (59 changesets), then no-op proof:**

```
59 changesets have not been applied to root@jdbc:mariadb://…/userservice
Liquibase command 'changelog-sync' was executed successfully.
root@jdbc:mariadb://…/userservice is up to date
Database is up to date, no changesets to execute
```

**AgencyService — the dump genuinely lags the changelog (ends at 0018), so the
selective mode was used: `--mark-ran-count 19` then `update` really applied
0019–0022:**

```
23 changesets have not been applied to root@jdbc:mariadb://…/agencyservice
[baseline-sync] step 3/5: mark-next-changeset-ran x 19 (selective baseline)
...
# update then applied 0019_agency_admin_control … 0022_agency_address_contact
# post-check: table agency_admin_control exists, column agency.settings exists
root@jdbc:mariadb://…/agencyservice is up to date
```

Final `DATABASECHANGELOG`: all 23 rows present, and a re-run of the script
reported `is up to date` (idempotent).

**ConsultingTypeService — `--fresh` rebuild from an empty database:**

```
11 changesets have not been applied to root@jdbc:mariadb://…/consultingtypeservice
UPDATE SUMMARY
Run:                         11
Previously run:               0
```

`scripts/liquibase-baseline-sync.sh` is shellcheck-clean
(`koalaman/shellcheck:stable`).

---

## Appendix A — Observed state on the dev cluster (2026-07-04, read-only capture)

Captured ~20:10 UTC via `ssh root@178.105.70.64`, pod `caritas/oriso-mariadb-0`
(MariaDB, `SELECT`/`SHOW` only). A teammate was actively resolving schema drift
during deployments on this day — treat this snapshot as historical; **re-capture
at execution time** (section 5, step 1).

| Database | `DATABASECHANGELOG` rows | Newest `DATEEXECUTED` | Changelog entries in current dev master | Lock |
|---|---|---|---|---|
| tenantservice | 13 | 2026-06-11 (`0013 createTenantAdminControls`) | 23 | free |
| userservice | 49 | 2025-10-26 (`0047 addMatrixPasswordColumn`) | 59 | free |
| agencyservice | 18 | 2025-09-06 (`0011 agencyDemographics`) | 23 | free |
| consultingtypeservice | 10 | 2025-09-06 (`0005 topic_fallback_agency_id`) | 11 | free |

Signals in the data:

- All recorded `FILENAME` values use the `db/changelog/changeset/...` form —
  identical to what the pinned Docker CLI records with this runbook's search-path
  setup, so changeset identity is preserved.
- tenantservice has one row dated **2026-06-11** (`createTenantAdminControls`),
  i.e. months *after* Liquibase was disabled — evidence that changelog rows have
  been added out-of-band/manually at least once. This is exactly why
  `clear-checksums` is part of the procedure.
- No rows dated 2026-07-04 at capture time: that day's drift-fixing had not
  (yet) touched the `DATABASECHANGELOG` tables.
- The schemas contain many post-Feb-2026 hand-applied objects with no changelog
  rows, e.g. userservice `agency_invite_link`, `case_handover_request`,
  `case_handover_reason_policy`, `identity_tombstone`,
  `counselor_rename_audit_log`, `event_notification`; tenantservice
  `tenant_admin_controls`. These correspond to the L1 catch-up changesets and are
  what `changelog-sync` will mark as ran.

Full table lists per database (capture, `SHOW TABLES`):

- **tenantservice:** DATABASECHANGELOG, DATABASECHANGELOGLOCK, sequence_tenant,
  sequence_tenant_admin_controls, tenant, tenant_admin_controls
- **userservice:** DATABASECHANGELOG, DATABASECHANGELOGLOCK, admin, admin_agency,
  agency_invite_link, appointment, case_handover_reason_policy,
  case_handover_request, chat, chat_agency, consultant, consultant_agency,
  consultant_mobile_token, consultant_topic, counselor_rename_audit_log,
  draft_message, event_notification, group_chat_participant, identity_tombstone,
  inactive_account_notification_audit_log, language, sequence_admin_agency,
  sequence_chat, sequence_chat_agency, sequence_consultant_agency,
  sequence_consultant_mobile_token, sequence_consultant_topic, sequence_session,
  sequence_session_data, sequence_session_topic, sequence_user_agency,
  sequence_user_chat, sequence_user_mobile_token, session, session_data,
  session_supervisor, session_topic, user, user_agency, user_chat,
  user_mobile_token
- **agencyservice:** DATABASECHANGELOG, DATABASECHANGELOGLOCK, agency,
  agency_admin_control, agency_postcode_range, agency_topic, diocese,
  sequence_agency, sequence_agency_admin_control, sequence_agency_postcode_range,
  sequence_agency_topic, sequence_diocese
- **consultingtypeservice:** DATABASECHANGELOG, DATABASECHANGELOGLOCK,
  sequence_topic, sequence_topic_group, sequence_topic_group_x_topic, topic,
  topic_group, topic_group_x_topic
