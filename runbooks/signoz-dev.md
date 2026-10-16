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
helm upgrade caritas . -n caritas -f secrets.yaml -f /root/observability-dev.yaml \
  --dry-run=server > /root/observability-dry.out   # read the diff first
helm upgrade caritas . -n caritas -f secrets.yaml -f /root/observability-dev.yaml \
  --wait-for-jobs --timeout 15m
```

The first rollout restarts Keycloak, Synapse (`Recreate`, a short Matrix gap),
Redis, the four Java services, Frontend and Admin. Announce it.

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
