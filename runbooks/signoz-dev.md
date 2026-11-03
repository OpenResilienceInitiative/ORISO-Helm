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

The upgrade below does not use `--reuse-values` on purpose. The release's
user-supplied values are `values.yaml` plus `secrets.yaml` (measured on
revisions 47 and 48, 2026-11-02), plus the observability overlay once this
rollout has run. If a later revision was upgraded with extra `-f` files, pass
them too.

So, right before the upgrade, list every user-supplied value path that the
planned `-f` files (`values.yaml`, `secrets.yaml`, then the overlay) would drop
or change.
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
files = ["values.yaml", "secrets.yaml", "/root/observability-dev.yaml"]
planned = {}
for name in files:
    planned.update(leaves(yaml.safe_load(open(name))))
overlay = leaves(yaml.safe_load(open(files[-1])))
live = leaves(json.load(sys.stdin))
drift = sorted(p for p in live if p not in planned or (p not in overlay and live[p] != planned[p]))
print(len(drift), *drift, sep="\n")' | tee /root/observability-drift.out
```

Stop unless the first line is `0`; otherwise add the listed paths' files with
`-f` first. Then record the running images, because the deploy scripts run
workloads on image digests and an upgrade resets them to the chart's tags:

```bash
kubectl get deploy -n caritas -o json | python3 -c '
import json, sys
for d in json.load(sys.stdin)["items"]:
    for c in d["spec"]["template"]["spec"]["containers"]:
        print(d["metadata"]["name"], c["name"], c["image"])' > /root/observability-images-before.txt
```

The upgrade commands check the drift themselves, and the live upgrade also
needs a successful server-side dry run, whose output you read first. The dry
run output contains rendered Secrets, so keep it private and delete it after
reading:

```bash
rm -f /root/observability-dry.ok
test "$(head -n 1 /root/observability-drift.out)" = 0 && \
(umask 077; helm upgrade caritas . -n caritas -f values.yaml -f secrets.yaml -f /root/observability-dev.yaml \
  --dry-run=server > /root/observability-dry.out) && touch /root/observability-dry.ok
# read /root/observability-dry.out, delete it, then:
test "$(head -n 1 /root/observability-drift.out)" = 0 && test -f /root/observability-dry.ok && \
helm upgrade caritas . -n caritas -f values.yaml -f secrets.yaml -f /root/observability-dev.yaml \
  --wait --timeout 20m
```

`--wait` works on Dev again since `userservice-report` was removed: revision 49
passed the wait in about 2 minutes on 2026-11-03.

If a `post-upgrade` hook fails, Helm marks the revision `failed` and skips the
remaining hooks, although all objects are applied. Revision 49 stopped at
`keycloak-reconcile-service-identities`; the cause is unknown because its logs
were unreachable. Read `helm get hooks` for the failed hook and the ones with a
higher `helm.sh/hook-weight`, and run each once more as a plain Job. Started this
way at 12:00 UTC, that hook, `keycloak-reconcile-smtp` and
`keycloak-reconcile-mail-locales` all completed. The next upgrade removes the
failed Job.

The hook runs before the images below are put back, so it talks to Keycloak on the
chart's image tag. On revision 49 it failed four times within 80 seconds, each in
2 to 3 seconds, although `--wait` had passed. Started again after the Dev image was
back, it passed. The cause is not proven. If the hook fails, put the images back
first and then start it again.

After the upgrade, put the recorded images back:

```bash
while read -r deploy container image; do
  current=$(kubectl get deploy "$deploy" -n caritas \
    -o jsonpath="{.spec.template.spec.containers[?(@.name==\"$container\")].image}")
  [ "$current" = "$image" ] || kubectl set image "deploy/$deploy" "$container=$image" -n caritas
done < /root/observability-images-before.txt
kubectl get deploy -n caritas -o name | xargs -n 1 kubectl rollout status -n caritas --timeout=300s
```

The first rollout restarts Keycloak, Synapse (`Recreate`, a short Matrix gap),
Redis, the four Java services, Frontend and Admin. Announce it.

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

Known gap: nginx metrics do not reach SigNoz on Dev. The chart's scrape
annotations sit on its own controller template, but Dev's controller runs
outside the release in the namespace `ingress-nginx`, and the `oriso-apps`
scrape job only looks at pods in `caritas` (measured 2026-11-02).

Harmless error: the MongoDB receiver logs `failed to collect metric
mongodb.flushes.rate` every minute on Dev. The other `mongodb.*` metrics still
arrive.

## ClickHouse memory and old system tables

ClickHouse ran without a memory limit and the kernel killed it on 2026-11-03 when
the worker ran out of memory. Dev now requests 2 GiB and limits it to 4 GiB, and
ClickHouse caps itself at 90% of that limit (3.6 GiB). The first upgrade with this
setting restarts ClickHouse once; the volume stays.

Check it after the upgrade. The first command must show about 3.6 GiB. The second
shows the restart count, which must not rise:

```bash
kubectl -n caritas exec chi-caritas-clickhouse-cluster-0-0-0 -c clickhouse -- clickhouse-client --query "SELECT formatReadableSize(toUInt64(value)) FROM system.server_settings WHERE name = 'max_server_memory_usage'"
kubectl -n caritas get pod chi-caritas-clickhouse-cluster-0-0-0 -o jsonpath='{.status.containerStatuses[?(@.name=="clickhouse")].restartCount}'
```

Raise the limit if ClickHouse restarts or reports `MEMORY_LIMIT_EXCEEDED` for its
real tables during a load test.

When a ClickHouse upgrade changes a system log's schema, ClickHouse renames the old
table with a numeric suffix such as `trace_log_0` and stops writing to it. Nothing
reads these copies. List them first, then drop them:

```bash
kubectl -n caritas exec chi-caritas-clickhouse-cluster-0-0-0 -c clickhouse -- clickhouse-client --query "SELECT name, formatReadableSize(total_bytes) FROM system.tables WHERE database = 'system' AND match(name, '_log_[0-9]+\$')"
kubectl -n caritas exec chi-caritas-clickhouse-cluster-0-0-0 -c clickhouse -- bash -c "clickhouse-client --query \"SELECT 'DROP TABLE system.' || name || ' SYNC;' FROM system.tables WHERE database = 'system' AND match(name, '_log_[0-9]+\\\$') FORMAT TSVRaw\" | clickhouse-client --multiquery"
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

## Rollback

Turn single parts off in the generated overlay (`signozCollector.enabled`,
`global.observability.webVitals.enabled`, `appMetrics`, `dbMetrics`) and
upgrade again, or `helm rollback caritas <REVISION> -n caritas`. Rollback never
deletes the retained volumes or the backup claim.
