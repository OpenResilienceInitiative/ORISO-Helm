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

## Rollout

Live Dev is deployed from `/root/ORISO-Helm` with a hand-kept `values.yaml`
that is older than `values.yaml.default`, and `values-dev.yaml` also carries
deltas Dev does not run today (Matrix server name, dummy data). So the rollout
takes only the observability keys, defaults included:

```bash
ssh dev-server
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
cd /root/ORISO-Helm && git fetch origin && git checkout <merged dev commit>
python3 scripts/observability_overlay.py values-dev.yaml > /root/observability-dev.yaml
```

The upgrade below does not use `--reuse-values` on purpose: the chart directory's
own `values.yaml` carries the live settings, and the release's user-supplied
values are exactly `secrets.yaml` (checked on revision 41, 2026-10-17), plus
the observability overlay once this rollout has run. If a later revision was
upgraded with extra `-f` files, pass them too. Without them, their settings
fall back to `values.yaml`.

So, right before the upgrade, list every user-supplied value path that the
planned `-f` files (`secrets.yaml`, then the overlay) would drop or change.
Paths the new overlay sets are the intended change and only count when it
drops them. It compares values without printing them:

```bash
helm get values caritas -n caritas -o json | python3 -c '
import json, sys, yaml
def leaves(d, p=""):
    out = {}
    for k, v in (d or {}).items():
        out.update(leaves(v, f"{p}.{k}") if isinstance(v, dict) and v else {f"{p}.{k}": v})
    return out
files = ["secrets.yaml", "/root/observability-dev.yaml"]
planned = {}
for name in files:
    planned.update(leaves(yaml.safe_load(open(name))))
overlay = leaves(yaml.safe_load(open(files[-1])))
live = leaves(json.load(sys.stdin))
drift = sorted(p for p in live if p not in planned or (p not in overlay and live[p] != planned[p]))
print(len(drift), *drift, sep="\n")' | tee /root/observability-drift.out
```

Stop unless the first line is `0`; otherwise add the listed paths' files with
`-f` first. The first rollout restarts Keycloak, Synapse (`Recreate`, a short
Matrix gap), Redis, the four Java services, Frontend and Admin. Announce it
before starting the upgrade.

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

Require a ready row with a start time for each ClickHouse pod. The live upgrade
also needs a successful server-side dry run, whose output you read first:

```bash
rm -f /root/observability-dry.ok
test "$(head -n 1 /root/observability-drift.out)" = 0 && \
helm upgrade caritas . -n caritas -f secrets.yaml -f /root/observability-dev.yaml \
  --dry-run=server > /root/observability-dry.out && touch /root/observability-dry.ok
# read /root/observability-dry.out, then:
test "$(head -n 1 /root/observability-drift.out)" = 0 && test -f /root/observability-dry.ok && \
UPGRADE_STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ) && \
helm upgrade caritas . -n caritas -f secrets.yaml -f /root/observability-dev.yaml \
  --wait --wait-for-jobs --timeout 15m
```

The retain hook only sees volumes that exist when Helm renders. After an
upgrade that adds ClickHouse shards or replicas, ZooKeeper or PostgreSQL
members, run the same `helm upgrade` once more, so the new volumes are switched
to `Retain` too.

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

## signoz.db backup and restore

The CronJob `caritas-signoz-data-protection-backup` runs at 02:30 UTC and keeps
14 gzipped copies in the claim `caritas-signoz-data-protection-backup`. Run one
now with:

```bash
kubectl -n caritas create job --from=cronjob/caritas-signoz-data-protection-backup signoz-backup-manual
```

Restore (SigNoz down for about a minute):

```bash
kubectl -n caritas scale statefulset caritas-signoz --replicas=0
# start a pod on the same node that mounts both claims, then inside it:
#   gunzip -c /backup/signoz-<stamp>.db.gz > /signoz/signoz.db && rm -f /signoz/signoz.db-wal /signoz/signoz.db-shm
kubectl -n caritas scale statefulset caritas-signoz --replicas=1
```

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

The setting stops new rows, but the existing table stays on disk. After the
rollout and explicit approval for the table drop, the operator removes it by
hand, using the same wait-and-confirm steps as `metric_log` above (rollout
finished, configuration active, one pod at a time):

```bash
POD=chi-caritas-clickhouse-cluster-0-0-0
kubectl -n caritas exec "$POD" -c clickhouse -- clickhouse extract-from-config --config-file /etc/clickhouse-server/config.xml --key trace_log
kubectl -n caritas exec "$POD" -c clickhouse -- clickhouse-client --query "SELECT name, formatReadableSize(total_bytes) FROM system.tables WHERE database = 'system' AND match(name, '^trace_log(_[0-9]+)?$')"
kubectl -n caritas exec "$POD" -c clickhouse -- clickhouse-client --query "DROP TABLE IF EXISTS system.trace_log SYNC"
```

The configuration command must print `Not found: trace_log` before the drop.
Repeat for each ClickHouse pod. Afterwards `system.trace_log` must not exist
and the Dev disk use of ClickHouse falls by about 8 GiB. To resume profiler
logging, set the contents of `config.d/99-oriso-disable-trace-log.xml` in
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

Turn single parts off in the generated overlay (`signozCollector.enabled`,
`global.observability.webVitals.enabled`, `appMetrics`, `dbMetrics`) and
upgrade again, or `helm rollback caritas <REVISION> -n caritas`. Rollback never
deletes the retained volumes or the backup claim.
