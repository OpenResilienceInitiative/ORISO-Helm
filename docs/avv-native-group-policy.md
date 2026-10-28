# AVV group admission companion

The companion closes the native Matrix JOIN boundary of
[AVV294](https://github.com/OpenResilienceInitiative/ORISO-TenantService/issues/294).
It is opt-in and requires the paired UserService context, snapshot and final-deletion
callers. Installing source or passing the local tests does not activate it on Dev.

## Policy and authority

TenantService remains the authority for the current governing AVV. UserService owns
the persisted group, serving organisation and room lineage. Synapse supplies proof
of actual applied participation. Registration, assignment and invitation are not
participation. Earlier actual participants can continue at subsequent occurrences;
new participants require the serving organisation's permission to begin counselling.

The module only removes its AVV veto. Synapse still enforces ordinary authentication,
invitation, ban and room permissions. Unknown/non-counselling rooms remain under
their normal rules. A verifier failure is sanitized 502, distinct from the explicit
legal 403. Existing exact-room participants can continue without asking a failed owner
service to re-authorize already begun work.

## Paired configuration

The chart leaves `matrix.groupPolicy.enabled=false`. To enable it, an operator must
first verify that the paired UserService is deployed and a dedicated Secret already
exists. Set `matrix.groupPolicy.existingSecret.name`, `tokenKey`, and `tokenRevision`.
The credential must be separate from Matrix admin, end-user, media and RTC credentials;
never put its value in Helm values, source, logs or the ticket. Change `tokenRevision`
when rotating it so both processes restart with the same token.

The supported process is exactly official Synapse 1.158.0, one main replica. The chart
rejects an enabled different version and the module checks the actual runtime version.
The portable storage path is tested with SQLite (the inspected Dev configuration) and
PostgreSQL. No database engine change or application Liquibase migration is introduced.
On initialization, the module creates two namespaced metadata tables in the existing
Synapse database. A coordinated deployment still requires backup/retention review and
verification of actual image, configuration and network policy on the target cluster.

`GroupPolicy` mounts through the module ConfigMap and `PYTHONPATH`.
`HistoryResource` is an `additional_resources` resource of a separate HTTP listener on
8801. It is never registered as a global web resource: Synapse attaches those to every
listener. The public client listener returns 404 for its path even with a valid token.
The private ClusterIP Service has no Ingress; NetworkPolicy permits 8801 only from
UserService pods in the same namespace. Existing client/media/metrics ports retain
their previous reachability. Render tests establish configuration; actual CNI
enforcement remains a deployment acceptance check.

The fixed callback target is `http://userservice:8080/internal/matrix/group-join-policy`.
The credential-bearing transport has a five-second bound, no redirects or web proxy,
strict matching responses, and bounded bodies. The existing public ingress patterns
do not route the callback's `/internal` path.

## Contracts

Both paths require the dedicated `X-Oriso-Group-Policy-Token` header. No API grants
ordinary room membership. All identifiers are checked and all responses remain bound
to the requested room and actor.

The existing UserService callback accepts v1 owner checks unchanged. The additive v2
request has exactly `contractVersion:2`, `operation:"CONTEXT"`, `roomId`, and
`matrixUserId`. Its response has exactly `contractVersion`, `roomId`, `matrixUserId`,
`applicable`, `roomIds`, and `historyComplete`. The context is pure persisted
UserService provenance, without legal/history HTTP calls or group-admission locks.
The module accepts at most 512 distinct canonical current/prior room IDs and 64 KiB;
excess or malformed context fails safely instead of truncating history.

The private history path is `/oriso-internal/group-participation-history`:

| Operation | Request | Successful response |
| --- | --- | --- |
| Actor history | v1, schema `synapse-1.158.0-applied-membership-v1`, room, actor | Same four fields plus `participation` (`COMMENCED` or `NOT_COMMENCED`) |
| Before recurring purge | v2, `SNAPSHOT_ROOM`, room | v2, same room, `complete:true` |
| True final group deletion | v2, `FORGET_ROOM`, room | v2, same room, `complete:true` |

Requests are limited to 4096 bytes. Unknown fields, duplicate keys, invalid JSON,
wrong versions and unsuitable identifiers are rejected. Missing/wrong credentials
are 401. Unusable storage or incomplete history coverage is 502, never a made-up
`NOT_COMMENCED`.

## Applied history, normal purge and privacy

`room_memberships` alone includes unsuccessful candidates. Positive proof requires a
forward, accepted, non-outlier, non-partial, non-soft-failed self-JOIN whose event was
actually installed in `current_state_delta_stream`. The module cross-checks stored
event data and only includes active local users. It does not cache negative decisions.

The official 1.158 storage source records applied current-state transitions. Its room
purge implementation explicitly does not clean the state-delta stream, but the actual
event rows are removed by room purge. Therefore the companion stores positive receipts
before the existing recurring-room purge, including participants who previously LEFT.
Each receipt contains only room ID, actor ID and applied event ID; it contains no
message text. The normal message purge remains unchanged. `on_new_event` also records
fresh JOINs only for already scoped counselling rooms and only after verifying their
actual application in the database. Exact-room continuation checks are read-only
until an applicable bound owner context or authenticated UserService history request
establishes that scope. Ordinary/internal Matrix rooms do not acquire AVV receipts.

A snapshot marks coverage complete only with verifiable room origin and complete
membership evidence. It fails if historical application records or event data are
missing. Already purged rooms without a prior complete snapshot remain unavailable.
Known positive receipts can still prove continuation when other legacy data is
incomplete. Legacy lineage already destroyed before this rollout cannot be recovered
from aliases, assignments or the new ledger. Such cases retain an explicit acceptance
gap; do not claim complete legacy coverage.

`FORGET_ROOM` deletes receipts and retains only an opaque retired-room marker. The
marker prevents a late applied-event callback or snapshot from resurrecting actor
data. True final group deletion calls it for the current, prior and prepared rooms;
ordinary recurrence must use `SNAPSHOT_ROOM`, never `FORGET_ROOM`.

Native `on_user_deactivation_status_changed` removes the actor's receipts. Snapshot
queries exclude deactivated users. Receipt writers serialize with deactivation:
PostgreSQL locks the native user row for share, and SQLite uses its serialized write
transaction. This ensures an in-flight write commits before native cleanup, while a
late callback cannot reinsert a deactivated actor. The tests exercise native
deactivation concurrently with the actual receipt writer, then a late write and a
later snapshot.

Synapse commits native deactivation before notifying modules and logs callback
failures without retrying them. Therefore an immediate startup reconciliation and
a ten-second background retry remove only module-owned receipts for missing or
deactivated native users. Failures are logged by category/type and remain pending;
they do not change the already committed native account state. A stale inactive
receipt is never usable as participation proof while cleanup is pending. Cleanup is
eventual after database recovery, not a claim of instantaneous erasure during an
outage. The real-database tests fail the first receipt DELETE, complete native
deactivation, recover the database and observe cleanup without a second request;
they also restart with a stale receipt and verify startup reconciliation.

Before unloading or rolling back the companion, retire its retained receipts through
the supported cleanup while the private resource/native cleanup hook is still
available: stop protected new work, retire the relevant groups, and verify that no
actor receipts remain before unload. Unload/rollback removes automatic native actor
cleanup and admission enforcement. Disabling the flag is not evidence that existing
metadata was removed; older pre-feature source already lacks the native gate.
Never drop the metadata tables or fabricate complete coverage to make an acceptance
run green. Backup restoration, manual history pruning and retirement must preserve
the distinction between positive proof, complete negative coverage and unknown history.

## Verification

Run the chart contract, then the actual pinned runtime on both databases:

```sh
python3 tests/render_matrix_group_policy_test.py
python3 tests/synapse_group_policy_bootstrap_recovery.py
python3 tests/synapse_group_policy_integration.py --database=sqlite
python3 tests/synapse_group_policy_integration.py --database=postgres
```

The integration fixture has isolated containers, a throwaway database, synthetic
actors and loopback-only published ports. Its external owner API is a controlled
contract stub. Synapse, native REST, room state, applied history and purge are real.
It proves pre-module JOIN/LEAVE, expiry denial, later continuation after actual v2
purge, newcomer refusal, retry, ordinary ban, private listener/auth/schema limits,
verification failures, native deactivation and final cleanup. Retention/corruption
probes deliberately alter only the disposable test database. It never accesses the
live cluster or real credentials and closes all owned containers/network/volume.

The bootstrap recovery regression runs the real module and Twisted inside the
same pinned Synapse image, with an explicitly substituted database interaction
boundary. It proves that a first schema failure returns unavailable, concurrent
consumers share one retry after recovery, and verification and receipt cleanup
resume without restart. Successful schema readiness remains cached. This probe
is distinct from the real-database deactivation DELETE failure/recovery tests.

For the behavioral baseline, `--without-module` deliberately fails: the protected
native JOIN returns 200 where the test requires 403. This is the missing boundary,
not evidence that the configured candidate works on Dev. The normal CI step runs
both candidate database modes; it cannot turn a missing Docker into a skipped pass.

Primary implementation references (pinned 1.158 source):

- [Module API and supported callback registration](https://github.com/element-hq/synapse/blob/v1.158.0/synapse/module_api/__init__.py)
- [Native join callback semantics](https://github.com/element-hq/synapse/blob/v1.158.0/synapse/module_api/callbacks/spamchecker_callbacks.py)
- [Applied membership/current-state persistence](https://github.com/element-hq/synapse/blob/v1.158.0/synapse/storage/databases/main/events.py)
- [Room purge and retained state-delta stream](https://github.com/element-hq/synapse/blob/v1.158.0/synapse/storage/databases/main/purge_events.py)
- [Native deactivation transaction and callback order](https://github.com/element-hq/synapse/blob/v1.158.0/synapse/handlers/deactivate_account.py)
