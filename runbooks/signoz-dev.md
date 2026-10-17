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
`-f` first. The upgrade commands check it themselves:

```bash
test "$(head -n 1 /root/observability-drift.out)" = 0 && \
helm upgrade caritas . -n caritas -f secrets.yaml -f /root/observability-dev.yaml \
  --dry-run=server > /root/observability-dry.out   # read the diff first
test "$(head -n 1 /root/observability-drift.out)" = 0 && \
helm upgrade caritas . -n caritas -f secrets.yaml -f /root/observability-dev.yaml \
  --wait --wait-for-jobs --timeout 15m
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
