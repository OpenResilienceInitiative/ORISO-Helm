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
`-f` first. The upgrade commands check it themselves, and the live upgrade
also needs a successful server-side dry run, whose output you read first:

```bash
rm -f /root/observability-dry.ok
test "$(head -n 1 /root/observability-drift.out)" = 0 && \
helm upgrade caritas . -n caritas -f secrets.yaml -f /root/observability-dev.yaml \
  --dry-run=server > /root/observability-dry.out && touch /root/observability-dry.ok
# read /root/observability-dry.out, then:
test "$(head -n 1 /root/observability-drift.out)" = 0 && test -f /root/observability-dry.ok && \
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

## Agent alert forwarding on Dev

Use this path after the first confirmed Slack alert and after the
Dreambau receiver, private encrypted route, and identities have been
approved. Keep `oriso-alerts-slack` as the Slack channel. The managed
SigNoz provisioner checks both channel names and types before it writes
rules. [First Slack delivery](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/468) · [Dreambau receiver](https://github.com/Storypapst/dreambau/pull/223)

### Stage the authenticated webhook

With agent forwarding off, mount the existing approved webhook Secret in
the SigNoz application container. Append the following entries to any
existing volume lists in the approved Dev override.

```yaml
signoz:
  signoz:
    additionalVolumeMounts:
      - name: oriso-agent-webhook-auth
        mountPath: /var/run/oriso-signoz-agent
        readOnly: true
    additionalVolumes:
      - name: oriso-agent-webhook-auth
        secret:
          secretName: <approved-dev-webhook-secret>
          defaultMode: 0444
          items:
            - key: token
              path: token
```

Confirm the file is readable from the SigNoz container using a file
existence or permission check. A SigNoz administrator creates
`oriso-alerts-agents` through `POST /api/v1/channels` with the approved
private endpoint and the mounted credential reference.

```json
{
  "name": "oriso-alerts-agents",
  "webhook_configs": [{
    "url": "https://<approved-private-host>/signoz/alerts",
    "send_resolved": false,
    "http_config": {
      "follow_redirects": false,
      "authorization": {
        "type": "Bearer",
        "credentials_file": "/var/run/oriso-signoz-agent/token"
      }
    }
  }]
}
```

Use the file reference because SigNoz v0.135.1 stores inline credentials
in the channel data that Viewers can read. Verify the approved endpoint,
peer and authentication from the Dev route. [Pinned channel API](https://github.com/SigNoz/signoz/blob/v0.135.1/pkg/apiserver/signozapiserver/alertmanager.go#L13-L56) · [Pinned credential serialization](https://github.com/SigNoz/signoz/blob/v0.135.1/pkg/types/alertmanagertypes/config.go#L509-L515)

After creating or updating a channel, allow an Alertmanager configuration
sync before running the rule test. SigNoz v0.135.1 polls every minute by
default. The channel list can show the saved route before the notifier has
loaded it. [Pinned sync interval](https://github.com/SigNoz/signoz/blob/v0.135.1/pkg/alertmanager/config.go#L38-L44)

### Enable and verify

Supply a fixed service name for every catalog rule in the forwarder's
`rule_services` map. The first four metrics are emitted by UserService.

```json
{
  "rule_services": {
    "provisioning-compensation-failure": "userservice",
    "availability-store-failure": "userservice",
    "notification-failure": "userservice",
    "unencrypted-room": "userservice",
    "clickhouse-capacity": "clickhouse",
    "collector-staleness": "collector"
  }
}
```

Set `signoz.orisoObservability.agentForwarding=true` in the approved Dev
override and run the managed provisioning Job. It adds the agent webhook
alongside Slack for the six rules in the mapping above. It emits the
catalog rule slug and unitless value the receiver expects. Additional
managed rules keep their Slack route. On the workstation use the Dev API
key from the approved secret source, then read back the rules.

```sh
python3 files/signoz/signoz_observability.py --mode check --skip-telemetry \
  --api-url https://dev.oriso.org/signoz --environment dev --cluster-name oriso-dev \
  --agent-forwarding
```

For the approved delivery test, choose a catalog rule with a numeric
sample in Dev. Run `--mode test-rule --rule <slug> --agent-forwarding`
with the same target arguments. A positive `alertCount` confirms rule
evaluation. Verify the Slack message, the Buzz room entry, and the
dedicated OpenClaw run separately. Check the delivered content for the
expected rule, service and value. The generic SigNoz channel test lacks
the catalog fields the receiver requires.

### Restore Slack-only routing

Set `agentForwarding=false` and run the managed provisioning Job again
with `signoz.orisoObservability.enabled=true`. Confirm all six stored
thresholds use Slack alone and the forwarding fields are absent. Check
Slack delivery before retiring the webhook and the Secret mount. A flag
change can alter the fingerprint of active alerts because their labels
change.

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

The setting stops new rows, but an existing table keeps merging. Helm `--wait` does not wait for the
ClickHouse operator, which restarts ClickHouse after the upgrade. Drop only after
the restart is done and the configuration is active, otherwise the old writer can
create the table again on its next flush:

```bash
kubectl -n caritas rollout status statefulset/chi-caritas-clickhouse-cluster-0-0 --timeout=10m
kubectl -n caritas get pods -o name | grep '^pod/chi-caritas-clickhouse-cluster-'
```

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

## Rollback

Turn single parts off in the generated overlay (`signozCollector.enabled`,
`global.observability.webVitals.enabled`, `appMetrics`, `dbMetrics`) and
upgrade again, or `helm rollback caritas <REVISION> -n caritas`. Rollback never
deletes the retained volumes or the backup claim.
