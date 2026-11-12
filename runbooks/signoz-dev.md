# SigNoz on Dev

What Dev runs, how to roll it out, how to prove it works, and how to get the
SigNoz database back. Collector internals: `signoz-k8s-infra.md`.

## What Dev runs

`values-dev.yaml` is the source of truth for Dev observability:

| Setting | Dev | Effect |
| --- | --- | --- |
| `signoz.enabled` | `true` | SigNoz UI at `https://dev.oriso.org/signoz`, OTLP export from all services |
| `global.observability.deploymentEnvironment` | `dev` | tag on every signal |
| `global.observability.tracingSamplingProbability` | `1.0` | every trace kept |
| `global.observability.appMetrics` / `dbMetrics` | `true` | nginx, Keycloak, Synapse, Redis, MariaDB, MongoDB, RabbitMQ |
| `global.observability.webVitals.enabled` | `true` | browser metrics at `/signoz-rum/v1/metrics` |
| `signozCollector.enabled` | `true` | node agent (pod logs) and cluster collector |
| `signoz.orisoDataProtection` | default on | volumes on `Retain`, daily `signoz.db` backup |
| `signoz.clickhouse.resources` | request 2 GiB, limit 4 GiB | ClickHouse cannot fill the worker |

A deployed Helm revision can be healthy while running without the Dev
observability overlay. Verify the overlay after every Dev install or upgrade:

```bash
helm get values caritas -n caritas --all -o yaml |
  python3 scripts/observability_overlay.py values-dev.yaml --check-effective
```

The check compares the keys declared by the Dev observability overlay and
prints only the paths of missing or different settings. A failure requires
the rollout procedure below and a fresh runtime check.

## Rollout

Before the first upgrade that renders the existing public alias, follow
[Adopt the public Dev alias](#adopt-the-public-dev-alias).

Live Dev is deployed from `/root/ORISO-Helm` with a hand-kept `values.yaml`
that is older than `values.yaml.default`, and `values-dev.yaml` also carries
deltas Dev does not run today (Matrix server name, dummy data). So the rollout
takes only the observability keys, defaults included:

```bash
ssh dev-server
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
cd /root/ORISO-Helm && git fetch origin && git checkout <merged dev commit>
observability_file=/root/observability-dev.yaml
python3 scripts/observability_overlay.py values-dev.yaml > "$observability_file"
values_files=(values.yaml secrets.yaml "$observability_file")
```

Pass the hand-kept `values.yaml` explicitly so Helm preserves base settings
recorded as user-supplied in the installed revision. Add every other approved
values file to `values_files` in its intended order. The drift check and both
Helm commands use that same list.

Before the upgrade, check for omitted user-supplied paths and for changes
outside the observability projection. The check merges the values files in
order with Helm's rules (a later `null` removes the key) before comparing, and
prints only the path count and the affected paths. The preflight below runs it
again against the current files and the live release on every call.

```bash
values_drift() {
  helm get values caritas -n caritas -o json | python3 -c '
import json, sys, yaml
def merge(base, override):
    for k, v in (override or {}).items():
        if v is None:
            base.pop(k, None)
        elif isinstance(v, dict) and isinstance(base.get(k), dict):
            merge(base[k], v)
        else:
            base[k] = v
    return base
def leaves(d, p=""):
    out = {}
    for k, v in (d or {}).items():
        out.update(leaves(v, f"{p}.{k}") if isinstance(v, dict) and v else {f"{p}.{k}": v})
    return out
overlay_file, *files = sys.argv[1:]
merged = {}
for name in files:
    merge(merged, yaml.safe_load(open(name)))
planned = leaves(merged)
overlay = leaves(yaml.safe_load(open(overlay_file)))
live = leaves(json.load(sys.stdin))
drift = sorted(p for p in live if p not in planned or (p not in overlay and live[p] != planned[p]))
print(len(drift), *drift, sep="\n")' "$observability_file" "${values_files[@]}"
}
values_drift
```

Resolve the listed paths before continuing. Use the approved ordered values
files, image pin overrides and post-renderer for both the preview and upgrade.
Dev's application images can be updated independently of Helm. The image check
compares the current direct Helm-managed Deployments, StatefulSets, DaemonSets
and CronJobs with the proposed manifest, including their init containers.

The first rollout restarts Keycloak, Synapse (`Recreate`, a short Matrix gap),
Redis, the four Java services, Frontend and Admin. Announce it before starting
the upgrade.

Before an upgrade that disables or restores a ClickHouse system log, record
the operator task ID and each ClickHouse pod's name, UID and container start
time. Keep this baseline to confirm each pod restarted after the upgrade.

```bash
CLICKHOUSE_TASK_BEFORE=$(kubectl -n caritas get clickhouseinstallation caritas-clickhouse -o jsonpath='{.status.taskIDsStarted[0]}')
test -n "$CLICKHOUSE_TASK_BEFORE"
kubectl -n caritas get pods -o json | jq -er '
  .items[] | select(.metadata.name | startswith("chi-caritas-clickhouse-cluster-"))
  | .metadata as $pod
  | .status.containerStatuses[] | select(.name == "clickhouse")
  | [$pod.name, $pod.uid, .state.running.startedAt, .ready] | @tsv'
```

Require a ready row with a start time for each ClickHouse pod.

Run this preflight on the Dev master. For a staged rollout, set
`expected_observability` to the values file that declares its approved
observability settings.

```bash
helm_args=(caritas . -n caritas)
for file in "${values_files[@]}"; do
  helm_args+=(-f "$file")
done
# Include the approved image overrides and --post-renderer arguments here.
expected_observability=values-dev.yaml
preflight_signoz() (
  set +x
  set -uo pipefail
  drift=$(values_drift) || exit 1
  test "$(head -n 1 <<<"$drift")" = 0 || { printf '%s\n' "$drift"; exit 1; }
  preview=$(helm upgrade "${helm_args[@]}" --dry-run=server --hide-secret -o json) || exit 1
  printf '%s' "$preview" | jq -e '.config' |
    python3 scripts/observability_overlay.py "$expected_observability" --check-effective || exit 1
  printf '%s' "$preview" | jq -er '.manifest' |
    python3 scripts/check_helm_workload_images.py --namespace caritas --release caritas "$@"
)
preflight_signoz
```

Review the object changes, ClickHouse installation, retained claims and hook
Jobs separately. Keep image-only promotions outside the rollout window and
repeat the preflight immediately before the approved upgrade in the same shell.

```bash
preflight_signoz && UPGRADE_STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ) && \
  helm upgrade "${helm_args[@]}" --wait --wait-for-jobs --timeout 15m
```

The retain hook only sees volumes that exist when Helm renders. After an
upgrade that adds ClickHouse shards or replicas, ZooKeeper or PostgreSQL
members, run the same `helm upgrade` once more, so the new volumes are switched
to `Retain` too.

### Adopt the public Dev alias

The Dev overlay restores `https://signoz.oriso.org/` as a 302 redirect to
`https://dev.oriso.org/signoz/`. A clean Dev install creates the Ingress.
Certificate issuance requires the alias DNS to reach the Dev ingress
controller and the `letsencrypt-prod` ClusterIssuer to be Ready. Dev already
has the Ingress. Adopt it in the approved Dev upgrade window so the
Certificate keeps the same owner UID.

On the Dev master, with the same value files selected for the planned upgrade,
check that the rendered Ingress matches the live route. Then dry-run a patch
limited to the alias metadata. Include any additional approved value files in
the render.

```bash
set -o pipefail
alias_ingress=signoz-oriso-org-redirect
test "$(kubectl -n caritas get ingress caritas-signoz-ingress \
  -o jsonpath='{.spec.rules[0].host}')" = dev.oriso.org || exit 1
alias_live=$(kubectl -n caritas get ingress "$alias_ingress" -o json) || exit 1
alias_desired=$(helm template caritas . -n caritas -f secrets.yaml -f /root/observability-dev.yaml \
  --show-only templates/signoz/signoz-dev-alias-ingress.yaml |
  python3 -c 'import json, sys, yaml; print(json.dumps(yaml.safe_load(sys.stdin)))') || exit 1
alias_route() {
  jq -cS '{apiVersion, kind, spec, name:.metadata.name, namespace:.metadata.namespace,
    annotations:(.metadata.annotations | del(
      .["meta.helm.sh/release-name"], .["meta.helm.sh/release-namespace"],
      .["helm.sh/resource-policy"], .["kubectl.kubernetes.io/last-applied-configuration"]))}'
}
test "$(alias_route <<<"$alias_live")" = "$(alias_route <<<"$alias_desired")" || exit 1
jq -e '
  .metadata.deletionTimestamp == null and (.metadata.ownerReferences // [] | length == 0)
  and (.metadata.labels["app.kubernetes.io/managed-by"] // "Helm") == "Helm"
  and (.metadata.annotations["meta.helm.sh/release-name"] // "caritas") == "caritas"
  and (.metadata.annotations["meta.helm.sh/release-namespace"] // "caritas") == "caritas"
  and (.metadata.annotations["helm.sh/resource-policy"] // "keep") == "keep"
' <<<"$alias_live" >/dev/null || exit 1
alias_patch=$(jq -c '{metadata:{resourceVersion:.metadata.resourceVersion,
  labels:{"app.kubernetes.io/managed-by":"Helm"},
  annotations:{"meta.helm.sh/release-name":"caritas",
    "meta.helm.sh/release-namespace":"caritas","helm.sh/resource-policy":"keep"}}}' <<<"$alias_live")
kubectl -n caritas patch ingress "$alias_ingress" --type=merge --patch "$alias_patch" --dry-run=server
```

Record the Ingress and Certificate UIDs. After reviewing the patch, run the same
patch command without the dry-run flag, then run the normal upgrade preflight.
The resource version requires a fresh comparison if the live Ingress changes.
The first upgrade requires this ownership metadata for the existing Ingress.
The keep annotation is set in the same patch so a Helm rollback to an older
revision preserves the Ingress and its Certificate owner.

After the upgrade, verify that the Helm release manifest includes the alias,
the Ingress and Ready Certificate retain their UIDs, and the Certificate still
names that Ingress UID as its owner. Check both responses with certificate
validation enabled:

```bash
curl -fsS -o /dev/null -w '%{http_code} %{redirect_url}\n' https://signoz.oriso.org/
curl -fsSL -o /dev/null -w '%{http_code} %{url_effective}\n' https://signoz.oriso.org/
```

Expect 302 to `https://dev.oriso.org/signoz/` and then 200. If an upgrade stops
after the patch, preserve the existing Ingress and verify both responses before
retrying. The keep annotation also leaves the live Ingress in place when a
later render omits the alias. Retire the alias through a separately reviewed
removal of the Ingress and its public DNS record.

## Proof

ClickHouse, from the Dev master (heredoc, because quoting over ssh breaks):

```bash
kubectl -n caritas exec -i chi-caritas-clickhouse-cluster-0-0-0 -c clickhouse -- clickhouse-client <<'SQL'
SELECT resources_string['service.name'] AS service, count()
FROM signoz_logs.distributed_logs_v2
WHERE timestamp > toUnixTimestamp64Nano(now64() - INTERVAL 5 MINUTE)
GROUP BY service ORDER BY 2 DESC;
SELECT serviceName, any(resources_string['deployment.environment']), any(resources_string['service.version'])
FROM signoz_traces.distributed_signoz_index_v3
WHERE timestamp > now() - INTERVAL 15 MINUTE GROUP BY serviceName;
SQL
kubectl get pv | grep -E 'signoz|clickhouse|zookeeper'   # RECLAIM POLICY = Retain
```

`scripts/signoz_runtime_acceptance.py` (see `signoz-runtime-acceptance.md`)
remains the full gate.

## Replay reviewed CI test results

The optional `signoz.otelCollector.ciLogIngest.enabled` flag defaults to
`false`. Use a separately approved Dev overlay to enable it after the
ClickHouse write check and backup gate pass. The collector listens on
`127.0.0.1:4331` inside its Pod. A Kubernetes operator with access to
`pods/portforward` can reach it through a numeric Pod port.

Select a Ready Pod from the `caritas-signoz-otel-collector` Deployment that
uses the chart ConfigMap. Check that the Pod has its own network namespace
and review the running containers before forwarding. Keep the local forward
and any SSH relay on loopback.

```bash
kubectl -n caritas port-forward --address 127.0.0.1 "pod/$collector_pod" 14331:4331
```

In another terminal, submit only the sanitized output of the approved
[CI test exporter](https://github.com/OpenResilienceInitiative/ORISO-E2E/pull/117)
for the verified GitHub run, job and test selectors. Use the attempt-specific
sanitized JUnit artifact for the Dev nightly smoke cases. The reviewed case
and file strings are source metadata. Keep report contents and application data
out of them.

```bash
curl --fail --silent --show-error \
  -H 'Content-Type: application/json' --data-binary "@$sanitized_ci_payload" \
  http://127.0.0.1:14331/v1/logs
```

After the batch flush, verify the exact run and job IDs, test IDs, statuses
and observation times in stored logs where `oriso.signal.source = 'ci'`.
Then check the matching CI dashboard window. The collector can return HTTP
200 after dropping records. If a request was interrupted, check stored rows before
replaying it. The collector marker records admission through the operator
receiver. The operator verifies GitHub artifact provenance before sending.

Close the tunnel after verification. Use the approved overlay with the flag
set to `false` to retire the listener. Hosted GitHub Actions delivery needs
its own approved network and identity setup.

## nginx metrics (ingress-nginx)

The ingress controller runs in the namespace `ingress-nginx`, outside the Helm
release. `global.observability.externalIngressNginxNamespace: ingress-nginx`
(Dev) makes the cluster collector scrape its pod IPs on port 10254. The chart
cannot switch metrics on in the controller: it must run with
`--enable-metrics=true` (Dev does since 2026-11-06). The collector's cluster
ClusterRole already allows `list`/`watch` on pods in all namespaces.

After the rollout, in SigNoz open Metrics and search `nginx_ingress_controller_requests`;
data from the last 5 minutes must appear. If it does not, check the controller
args (`kubectl -n ingress-nginx get deploy ingress-nginx-controller -o yaml | grep enable-metrics`)
and the collector log for `ingress-nginx` scrape errors.

## ClickHouse memory and old system tables

ClickHouse ran without a memory limit and the kernel killed it on 2026-11-03 when
the worker ran out of memory. Dev now requests 2 GiB and limits it to 4 GiB, and
ClickHouse caps itself at 90% of that limit (3.6 GiB). The first upgrade with this
setting restarts ClickHouse once; the volume stays.

Before the upgrade, record the restart count of every ClickHouse pod:

```bash
kubectl -n caritas get pods -o name | grep '^pod/chi-caritas-clickhouse-cluster-' | while read -r pod; do
  echo "$pod $(kubectl -n caritas get "$pod" -o jsonpath='{.status.containerStatuses[?(@.name=="clickhouse")].restartCount}')"
done | tee /root/clickhouse-restarts-before.txt
```

After the upgrade, run it again and compare each pod with its own line. The
operator replaces the pod during the first upgrade, so a new pod starts at 0; any
count above 0 afterwards means ClickHouse restarted on its own. Each pod must show
about 3.6 GiB:

```bash
kubectl -n caritas get pods -o name | grep '^pod/chi-caritas-clickhouse-cluster-' | while read -r pod; do
  echo "$pod $(kubectl -n caritas get "$pod" -o jsonpath='{.status.containerStatuses[?(@.name=="clickhouse")].restartCount}')"
  kubectl -n caritas exec "${pod#pod/}" -c clickhouse -- clickhouse-client --query "SELECT formatReadableSize(toUInt64(value)) FROM system.server_settings WHERE name = 'max_server_memory_usage'"
done
```

Raise the limit if ClickHouse restarts or reports `MEMORY_LIMIT_EXCEEDED` for its
real tables during a load test.

When a ClickHouse upgrade changes a system log's schema, it may rename the old
table with a numeric suffix such as `trace_log_0`. List these tables on each pod
first. This is read-only:

```bash
kubectl -n caritas exec "$POD" -c clickhouse -- clickhouse-client --query "SELECT name, formatReadableSize(total_bytes) FROM system.tables WHERE database = 'system' AND match(name, '_log_[0-9]+\$')"
```

Review each candidate's age and usage before removal. Do not pipe the list into
`DROP`. Delete only confirmed obsolete Dev system tables, one at a time, on each
pod. For example:

```bash
kubectl -n caritas exec "$POD" -c clickhouse -- clickhouse-client --query "DROP TABLE IF EXISTS system.trace_log_0 SYNC"
```

## ClickHouse internal log retention

The Dev overlay sets a three-day deletion TTL for new `query_log`, `part_log`,
`trace_log`, `asynchronous_metric_log`, `metric_log`, `zookeeper_log`,
`aggregated_zookeeper_log` and `processors_profile_log` tables. Layered Pre-Dev
inherits it. `text_log` already has a three-day TTL in the chart. Later writer
removal files take precedence.

Restore writable headroom before the approved rollout. A changed engine
definition can make ClickHouse rename the old table to `<name>_0` or a higher
suffix at its next flush. The old table keeps its data and TTL. Restoring the
previous definition can rotate again. Measure storage reclamation separately.

Before the upgrade, record ClickHouse pod identities, the newest successful
backup and this read-only inventory for each data pod. After the operator
reconciles, verify a new Ready ClickHouse container and repeat the queries
after the configured log flush intervals. Require a three-day deletion TTL
for each enabled canonical writer, account for numbered tables and check
actual unreserved space before acceptance probes.

```bash
kubectl -n caritas exec "$POD" -c clickhouse -- clickhouse-client --readonly=1 --multiquery --query "
SELECT name, uuid, engine_full
FROM system.tables
WHERE database = 'system'
  AND replaceRegexpOne(name, '_[0-9]+$', '') IN (
    'query_log', 'part_log', 'text_log', 'trace_log', 'metric_log',
    'asynchronous_metric_log', 'zookeeper_log', 'aggregated_zookeeper_log',
    'processors_profile_log'
  )
ORDER BY name;
SELECT table, sum(bytes_on_disk) AS active_bytes
FROM system.parts
WHERE database = 'system' AND active
  AND replaceRegexpOne(table, '_[0-9]+$', '') IN (
    'query_log', 'part_log', 'text_log', 'trace_log', 'metric_log',
    'asynchronous_metric_log', 'zookeeper_log', 'aggregated_zookeeper_log',
    'processors_profile_log'
  )
GROUP BY table ORDER BY table;
SELECT name, total_space, unreserved_space FROM system.disks WHERE name = 'default';"
```

Check the recorded UUIDs to distinguish retained data from new canonical
tables. Keep old tables until their TTL expires or the operator separately
approves removal of specific obsolete tables.

## Replace an unavailable SQLite backup destination

Keep the original Helm-owned backup claim and its PV for storage recovery.
Provision a dedicated, empty, writable Filesystem PVC in `caritas` under the
approved storage recovery procedure. With a `WaitForFirstConsumer` StorageClass,
give an approved temporary consumer the backup CronJob's pod affinity and
tolerations. The scheduler can then bind the claim on the node running SigNoz
ordinal 0. Confirm the replacement is Bound to a Retain PV and can mount on
the SQLite source's eligible nodes. Allow space for the uncompressed
snapshot, the compressed copy in progress and retained archives.
Wait for the temporary consumer and any active backup Job to finish before
switching the destination.

Set the replacement PVC name in the approved Dev values:

```yaml
signoz:
  orisoDataProtection:
    backup:
      replacementClaim: signoz-backup-recovery
```

Dev's `hcloud-volumes` StorageClass provisions PVs with `Delete`. After the
approved mount check, set `Retain` on the PV bound to the approved replacement
claim. The patch verifies the PV identity and revision before changing the
reclaim policy.

```bash
backup_destination=signoz-backup-recovery
(
  set -euo pipefail
  pvc=$(kubectl -n caritas get pvc "$backup_destination" -o json)
  claim_uid=$(jq -er 'select(.status.phase == "Bound").metadata.uid' <<<"$pvc")
  volume=$(jq -er '.spec.volumeName' <<<"$pvc")
  pv=$(kubectl get pv "$volume" -o json)
  patch=$(jq -ce --arg claim "$backup_destination" --arg uid "$claim_uid" '
    select(.status.phase == "Bound" and .spec.claimRef.namespace == "caritas"
           and .spec.claimRef.name == $claim and .spec.claimRef.uid == $uid)
    | if .spec.persistentVolumeReclaimPolicy == "Retain" then []
      elif .spec.persistentVolumeReclaimPolicy == "Delete" then [
        {op: "test", path: "/metadata/uid", value: .metadata.uid},
        {op: "test", path: "/metadata/resourceVersion", value: .metadata.resourceVersion},
        {op: "test", path: "/spec/claimRef/uid", value: $uid},
        {op: "replace", path: "/spec/persistentVolumeReclaimPolicy", value: "Retain"}
      ] else error("Unexpected reclaim policy") end' <<<"$pv")
  if test "$patch" != '[]'; then
    kubectl patch pv "$volume" --type=json --patch "$patch"
  fi
  kubectl get pv "$volume" -o json |
    jq -er --arg uid "$claim_uid" 'select(.spec.claimRef.uid == $uid)
      | select(.spec.persistentVolumeReclaimPolicy == "Retain") | .spec.persistentVolumeReclaimPolicy'
)
```

Use the Dev Helm upgrade procedure and start with `--dry-run=server`. Helm
reads the original and replacement PVCs and the replacement PV. It requires
the original PVC to belong to this release with the `keep` policy and
checks that the replacement PV's claim reference matches the PVC's namespace,
name and UID. With an override, a client-only render fails. The chart keeps
rendering the original PVC, and the replacement remains independently managed.

Record the UTC start time before the upgrade.

```bash
backup_switch_started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
```

After the upgrade and the next scheduled run, check that both claims are
present. From the Helm checkout, read the archive receipt for the newest
scheduled Job since the switch and the creation of the current PVC.

```bash
backup_receipt=$(
  python3 scripts/signoz_metadata_backup_receipt.py \
    --namespace caritas --cronjob caritas-signoz-data-protection-backup \
    --claim "$backup_destination" --since "$backup_switch_started_at" --require-retain
)
```

The readback checks the Bound Retain PV against the claim UID, then requires
the selected Job and its successful backup Pod to use that destination.
The newest scheduled run determines acceptance. A manual run or an earlier
successful Job cannot satisfy it. Mount the reported claim at `/backup`
and verify the reported archive using the isolated restore below.

Clearing the override, or rolling back to a revision before the switch, can
send subsequent Jobs to the original claim. Confirm that destination can
attach before reusing it. Keep both claims available for recovery.

## signoz.db backup and restore

By default, the CronJob `caritas-signoz-data-protection-backup` runs at 02:30
UTC and keeps 14 verified gzip archives in its backup claim. The producer
checks the SQLite snapshot and gzip contents before publishing the archive,
then records the archive name, Job UID, Pod UID, compressed byte count and
SHA-256 in the backup container's Kubernetes termination message. It starts
pruning only after the archive and receipt have been written. If the Pod is
interrupted before Kubernetes records a successful exit, a published archive
can still be checked by explicit path. A scheduled handoff requires the
successful Pod receipt.
The archive contains SigNoz accounts, credentials and configuration, so keep
it on protected storage within the approved recovery environment.

Finish active backup Jobs before rolling out this producer or rolling back to
the legacy producer. The older script does not take the PVC lock. Retained
Kubernetes Job history is shorter than archive retention, so older archives
can be recovered by explicit path.

For an approved on-demand backup, confirm no backup is active and leave the
full Job deadline before the next scheduled start. The scheduled and manual
jobs share the same backup directory.

```bash
kubectl -n caritas create job --from=cronjob/caritas-signoz-data-protection-backup signoz-backup-manual
```

For an isolated SQLite restore, use a recovery pod with no service account
token and the backup CronJob's pinned Python image. Run it as UID 0, the Dev
SigNoz user and the owner of the new private archives. Use these mounts:

| Mount | Source | Access |
|---|---|---|
| `/backup` | Claim from the receipt, or the claim holding the selected historical archive | Read-only |
| `/scripts` | `caritas-signoz-data-protection` ConfigMap | Read-only |
| `/restore` | Fresh recovery claim in the same namespace | Read-write |

Schedule the recovery read away from the next backup window and wait for the
current backup pod to finish. Keep the recovery claim detached from SigNoz
while staging. Use the successful readback's archive name, checksum and byte
count. Set `recovery_pod` to the isolated Pod's name.

```bash
(
  set -euo pipefail
  archive=$(jq -er '.archive' <<<"$backup_receipt")
  digest=$(jq -er '.sha256' <<<"$backup_receipt")
  bytes=$(jq -er '.bytes' <<<"$backup_receipt")
  kubectl -n caritas exec "$recovery_pod" -- python3 /scripts/restore.py \
    "/backup/$archive" /restore --sha256 "$digest" --compressed-bytes "$bytes"
)
```

For explicit recovery without a retained receipt, pass the exact archive
path. The script verifies the embedded SHA-256 for newer names and also
accepts older timestamp-only archives.

```bash
archive=/backup/signoz-YYYYMMDDTHHMMSSZ.db.gz
python3 /scripts/restore.py "$archive" /restore
```

The script consumes the full gzip stream, checks SQLite integrity and the
SigNoz metadata tables, and publishes `/restore/signoz.db` as a private file
with mode `0600`. Use a recovery filesystem that supports hard links and an
empty destination. The script allows the filesystem's `lost+found` directory.
A complete staged database and the successful exit status are the
preconditions for the isolated application check.

Start the matching SigNoz version against the recovery claim in an isolated
stack with private UI access and outbound alert delivery blocked. Verify
authorized sign-in and the expected dashboard and rule configuration. Record
the selected archive, database check and application readback. Keep the active
claim available for the separately approved cutover or rollback.

The SQLite archive covers SigNoz metadata. ClickHouse signals have their own
backup and recovery requirements.

## ClickHouse system logs

Dev turns `system.metric_log` off. With 1552 columns its background merges
hit the memory limit about 6 times per second on Dev and used about 3 CPU cores.
Pausing only those merges for two minutes on 2026-11-03 took ClickHouse from 3.17
to 0.43 cores and the errors from 6.45 per second to 0.

The setting stops new rows, but an existing table keeps merging. After the
upgrade, require the completed configuration and the success log for the
latest started operator task. Operator 0.21.2 can assign a different
`status.taskID` when it writes the completed snapshot, so match the log
against `taskIDsStarted[0]`. Use the fragment for the system log you changed:

```bash
set -o pipefail
LOG_FRAGMENT=config.d/99-oriso-disable-metric-log.xml
# For trace_log, use config.d/99-oriso-disable-trace-log.xml.
CLICKHOUSE_RECONCILE_TASK=$(
  kubectl -n caritas get clickhouseinstallation caritas-clickhouse -o json |
  jq -er --arg before "$CLICKHOUSE_TASK_BEFORE" --arg fragment "$LOG_FRAGMENT" '
    .status as $status | .spec.configuration.files[$fragment] as $desired
    | ($status.taskIDsStarted // [])[0] as $started
    | select($before != "" and $desired != null and $status.status == "Completed"
      and $started != null and $started != "" and $started != $before
      and $status.taskID != null
      and (($status.taskIDsCompleted // []) | index($status.taskID)) != null
      and $status.normalizedCompleted.spec.configuration.files[$fragment] == $desired)
    | $started'
) &&
kubectl -n caritas logs deployment/caritas-clickhouse-operator -c operator \
  --since-time="${UPGRADE_STARTED_AT:?Missing upgrade start time}" |
  grep -F "caritas/caritas-clickhouse/$CLICKHOUSE_RECONCILE_TASK:reconcile completed successfully, task id: $CLICKHOUSE_RECONCILE_TASK"
```

Stop if this check cannot find a success line for the exact task. A completed
snapshot alone can also follow a failed reconciliation.

Run the pod identity command from [Rollout](#rollout) again. For every pod,
require `ready=true`, a new UID or container start time, and a start time
strictly later than `UPGRADE_STARTED_AT`. The operator task and pod checks must
both pass before inspecting the active files or dropping a table. Helm can
finish before reconciliation starts, and the config files can change before
the running server loads them.

For every listed pod, confirm `metric_log` is absent from the active configuration,
then inspect its local tables. ClickHouse may have renamed older versions with a
numeric suffix:

```bash
POD=chi-caritas-clickhouse-cluster-0-0-0
kubectl -n caritas exec "$POD" -c clickhouse -- clickhouse extract-from-config --config-file /etc/clickhouse-server/config.xml --key metric_log
kubectl -n caritas exec "$POD" -c clickhouse -- clickhouse-client --query "SELECT name, formatReadableSize(total_bytes) FROM system.tables WHERE database = 'system' AND match(name, '^metric_log(_[0-9]+)?$')"
```

The configuration command must print `Not found: metric_log`. Review the output
and confirm the listed tables are old, unused Dev system logs before deleting any
of them. Delete one explicitly at a time, then repeat the inspection for each
ClickHouse pod. For example:

```bash
kubectl -n caritas exec "$POD" -c clickhouse -- clickhouse-client --query "DROP TABLE IF EXISTS system.metric_log_0 SYNC"
```

### system.trace_log

Dev also turns `system.trace_log` off (`config.d/99-oriso-disable-trace-log.xml`).
Measured read-only on Dev on 2026-11-06: 8.21 GiB active, 338 M rows, about
1 GiB per day (the first day, 2026-11-03, was 5.6 GiB while the memory problems
ran). It is ClickHouse's own sampling-profiler output, already limited to 7
days by default, and no query in the retained `system.query_log` window
(2026-11-03 to 2026-11-06) selects from it. SigNoz does not use it.

The setting stops new rows and the existing table stays on disk. After the
rollout, confirm the completed restart and active configuration on each pod
using the wait-and-confirm steps for `metric_log` above. Run the read-only
inspection first.

```bash
POD=chi-caritas-clickhouse-cluster-0-0-0
kubectl -n caritas exec "$POD" -c clickhouse -- clickhouse extract-from-config --config-file /etc/clickhouse-server/config.xml --key trace_log
kubectl -n caritas exec "$POD" -c clickhouse -- clickhouse-client --query "SELECT name, formatReadableSize(total_bytes) FROM system.tables WHERE database = 'system' AND match(name, '^trace_log(_[0-9]+)?$')"
```

The configuration command must print `Not found: trace_log`. After this
readback and explicit approval for the table drop, run the removal separately.

```bash
kubectl -n caritas exec "$POD" -c clickhouse -- clickhouse-client --query "DROP TABLE IF EXISTS system.trace_log SYNC"
```

Repeat the inspection and approved removal for each ClickHouse pod. Verify
that `system.trace_log` is absent and check the available disk space. To resume
profiler logging, set the contents of `config.d/99-oriso-disable-trace-log.xml` in
`values-dev.yaml` to `<clickhouse/>`. Keeping the key with an empty config makes
this an explicit overlay change that the drift check can verify.

Regenerate the overlay and rerun the rollout drift check:

```bash
python3 scripts/observability_overlay.py values-dev.yaml > /root/observability-dev.yaml
```

In the server-side dry run, confirm that the fragment has no
`<trace_log remove="1"/>` directive. Record the pod identities before the
upgrade and confirm each ClickHouse restart using the procedure above. Then
verify that the active configuration reports `trace_log` on every pod:

```bash
kubectl -n caritas exec "$POD" -c clickhouse -- clickhouse extract-from-config --config-file /etc/clickhouse-server/config.xml --key trace_log.table
```

## Rollback

For a targeted reversal, use the reviewed previous chart and overlay with the
current image pins. Use the [rollout preflight](#rollout) for that forward
upgrade. For an infrastructure collector reversal, follow the
[collector rollback instructions](signoz-k8s-infra.md#release-gate-and-rollback).

A Helm rollback reapplies the saved manifest. Check the approved target
revision on the Dev master before using it.

```bash
set -o pipefail
helm history caritas -n caritas
helm get manifest caritas -n caritas --revision "${revision:?Set the approved target revision}" |
  python3 scripts/check_helm_workload_images.py --namespace caritas --release caritas
```

An image mismatch requires the forward upgrade with the current pins. Apply
the same check to the target of any automatic rollback before enabling it.
Review the rollback's resource removals and hook Jobs separately, and require
it to retain the SigNoz, ClickHouse, ZooKeeper and backup claims.
