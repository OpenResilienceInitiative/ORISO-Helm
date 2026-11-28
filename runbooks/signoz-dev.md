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
| `networkPolicies.clickhouseIngress.enabled` | `true` | limits ClickHouse ingress to its SigNoz clients |

A deployed Helm revision can be healthy while running without the Dev
observability overlay. Verify the overlay after every Dev install or upgrade:

```bash
helm get values caritas -n caritas --all -o yaml |
  python3 scripts/observability_overlay.py values-dev.yaml --check-effective
```

The check compares the keys declared by the Dev observability overlay, including
the ClickHouse ingress policy, and prints only paths of missing or different
settings. A failure requires
the rollout procedure below and a fresh runtime check.

## Rollout

Before the first upgrade that renders the existing public alias, follow
[Adopt the public Dev alias](#adopt-the-public-dev-alias).

Live Dev is deployed from `/root/ORISO-Helm` with a hand-kept `values.yaml`
that is older than `values.yaml.default`, and `values-dev.yaml` also carries
deltas Dev does not run today (Matrix server name, dummy data). So the rollout
takes only the observability keys and ClickHouse ingress policy, defaults included:

```bash
ssh dev-server
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
cd /root/ORISO-Helm && git fetch origin && git checkout <merged dev commit>
observability_file=/root/observability-dev.yaml
observability_inputs=(values-dev.yaml)
python3 scripts/observability_overlay.py "${observability_inputs[@]}" > "$observability_file"
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
For a separately approved ClickHouse signal backup, include its values in the
same projection and check as described in
[Activate the scheduled archives](signoz-clickhouse-recovery.md#activate-the-scheduled-archives).
Dev's application images can be updated independently of Helm. The image check
compares the current direct Helm-managed Deployments, StatefulSets, DaemonSets
and CronJobs with the proposed manifest, including their init containers.
The ClickHouse storage check compares the rendered data claim template with
the running installation and every data claim mounted by its owned Pods.

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

Run this preflight on the Dev master. Use the same approved
`observability_inputs` array that generated the rollout projection.

```bash
# Put the approved image overrides and --post-renderer arguments here.
extra_helm_args=()
# Rebuild the Helm arguments from the current `values_files`, so the drift
# check, the preview and the upgrade always use the same list.
signoz_helm_args() {
  helm_args=(caritas . -n caritas)
  for file in "${values_files[@]}"; do
    helm_args+=(-f "$file")
  done
  helm_args+=("${extra_helm_args[@]}")
}
preflight_signoz() (
  set +x
  set -uo pipefail
  signoz_helm_args
  drift=$(values_drift) || exit 1
  test "$(head -n 1 <<<"$drift")" = 0 || { printf '%s\n' "$drift"; exit 1; }
  preview=$(helm upgrade "${helm_args[@]}" --dry-run=server --hide-secret -o json) || exit 1
  printf '%s' "$preview" | jq -e '.config' |
    python3 scripts/observability_overlay.py "${observability_inputs[@]}" --check-effective || exit 1
  printf '%s' "$preview" | jq -er '.manifest' |
    python3 scripts/check_helm_workload_images.py --namespace caritas --release caritas "$@" || exit 1
  printf '%s' "$preview" | jq -er '.manifest' |
    python3 -m scripts.check_signoz_clickhouse_storage --namespace caritas --release caritas
)
preflight_signoz
```

Review the object changes, ClickHouse installation, retained claims and hook
Jobs separately. Keep image-only promotions outside the rollout window and
repeat the preflight immediately before the approved upgrade in the same shell.
If you change `values_files` or `extra_helm_args`, the next preflight and the
upgrade both pick up the change.

```bash
signoz_helm_args && preflight_signoz && \
  UPGRADE_STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ) && \
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

### Adopt the SigNoz MCP server

Since 2026-10-14, Dev has had a hand-applied `signoz-mcp-server` Deployment,
Service and Ingress at `https://dev.oriso.org/signoz-mcp`. That server held a
SigNoz API key, so anyone could use it without credentials. The Dev overlay now
renders the same three objects with `signoz.orisoMcpServer.enabled`. The image
is pinned, the server runs as non-root with a read-only root filesystem, and it
holds no API key. Each client sends its own SigNoz API key in a
`SIGNOZ-API-KEY` header, and requests without one get HTTP 401.

In the approved Dev upgrade window, give the three existing objects Helm
ownership so the upgrade replaces them instead of failing on existing objects:

```bash
for kind in deployment service ingress; do
  kubectl -n caritas label "$kind" signoz-mcp-server app.kubernetes.io/managed-by=Helm --overwrite
  kubectl -n caritas annotate "$kind" signoz-mcp-server \
    meta.helm.sh/release-name=caritas meta.helm.sh/release-namespace=caritas --overwrite
done
```

After the upgrade, revoke the SigNoz API key the old Deployment used
(**Settings → API Keys**). It was usable through the public route. Then check
from outside the cluster:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://dev.oriso.org/signoz-mcp/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

Expect 401. Clients configure the URL `https://dev.oriso.org/signoz-mcp/mcp`
with their own key in the `SIGNOZ-API-KEY` header.

### Operations surfaces on Dev (#304)

Dev observations for the DPIA technical and organisational measures annex.
The external readback was recorded on 2026-11-21. The MCP deployment was
checked separately through Kubernetes metadata on 2026-11-21 UTC.

| Surface | Control on Dev |
| --- | --- |
| SigNoz UI `/signoz` | SigNoz login. The page loads, but its API returns 401 without a session. Basic Auth cannot sit in front because SigNoz sends its own `Authorization: Bearer` header. |
| SigNoz MCP | Kubernetes reports the hand-applied Deployment as ready, and its configuration still includes a setting for a server-held key. After Helm adoption and rollout, the Deployment runs in keyless mode and clients authenticate with their own SigNoz keys. Revoke the old key and verify client authentication after rollout. |
| Storybook `/storybook-*` | The chart renders Basic Auth (`storybook.ingress.authSecret`). Live is still a hand-applied Ingress without auth until the `storybook-basic-auth` Secret exists and Helm owns the route. |
| HealthDashboard `/health` | Public status only, by design (ORISO-HealthDashboard#57). |
| Redis Commander, Status page | Not deployed on Dev. Both paths fall through to the App. |

### External HTTPS, TLS expiry and liveness

Run the credential-free probe at least hourly on a runner outside the Dev
cluster and its hosting provider.

```sh
python3 scripts/signoz_external_uptime.py
```

The probe verifies TLS trust and hostname on each connection. It requires the
public alias to redirect to `https://dev.oriso.org/signoz/` and checks that
page directly. It also requires HTTP 200 and JSON status `ok` at
`/signoz/api/v1/health?live=1`. The pinned SigNoz handler checks its
ClickHouse connection on that path. Stored telemetry and authenticated use
have their own acceptance checks.

Use the runner's system trust store or pass it with `--ca-file`. The JSON
output includes UTC certificate expiry for both hosts. The exit codes follow
the monitoring plugin convention.

| Exit | Result | Meaning |
| --- | --- | --- |
| 0 | OK | Both public routes and liveness pass, and both certificates have at least 21 days left. |
| 1 | WARNING | A certificate has fewer than 21 days left (S7 P2). |
| 2 | CRITICAL | A public check failed or a certificate has fewer than seven days left (S7 P1 for expiry). |
| 3 | UNKNOWN | The probe could not initialize its configured trust store. |

Configure the external runner to deliver incidents and recovery to the
approved operations destination. The SigNoz heartbeat uses a separate
receiver and tracks whether the collector continues to supply samples.

#### Schedule the check on Linux

The systemd service runs once a minute with a transient account and a
45-second deadline. It uses the host trust store and allows outbound DNS
and HTTPS. The service needs Python 3.10 or newer and systemd 246 or newer.
[Unit files](systemd/)

On the approved runner, install `scripts/signoz_external_uptime.py` as
`/opt/oriso/signoz-uptime/signoz_external_uptime.py`. Make the path and file
readable by the service and writable only by the operator. Install
`oriso-signoz-uptime.service` and `oriso-signoz-uptime.timer` from
`runbooks/systemd` in `/etc/systemd/system`, then run these commands.

```sh
systemd-analyze verify /etc/systemd/system/oriso-signoz-uptime.service \
  /etc/systemd/system/oriso-signoz-uptime.timer
systemctl daemon-reload
systemctl enable --now oriso-signoz-uptime.timer
systemctl show oriso-signoz-uptime.service \
  -p Result -p ExecMainCode -p ExecMainStatus -p ExecMainExitTimestamp
journalctl -u oriso-signoz-uptime.service -n 10 --no-pager
```

Use the timed result of a completed invocation for incident and recovery
delivery. Monitor missed executions from outside the runner so a stopped
timer or host is also detected.

#### Read completed checks for the incident consumer

Run the read-only journal reader from an account with access to the local
system journal. It reads the two check units above and in the
[heartbeat receiver runbook](signoz-heartbeat-receiver.md). It uses
systemd's structured manager events, verified on systemd 255. Keep the
provided oneshot definitions and their default successful exit status.

```sh
python3 scripts/signoz_check_journal.py --since '10 minutes ago'
python3 scripts/signoz_check_journal.py --cursor "$last_accepted_cursor"
```

Each page contains up to 24 completed starts in journal order. It includes
normal probe exit statuses, failed starts and timeouts. Missing completion
details map to `UNKNOWN`. Use machine, boot, unit and invocation ID to
deduplicate retries. Use the completion cursor as the identifier when
systemd reports a job failure without an invocation ID. Keep the
original probe JSON for diagnostics.

Store `next_cursor` only after the destination accepts the page and keep
fetching while `more` is true. If the start window is empty, keep it until
the first cursor is available. A missing resume cursor exits 3 so the
operator can investigate a journal gap. Treat missing or stale results as
monitoring failures. A successful read with an empty page provides no
health result.

The [off-site backup monitoring scope](offsite-backup.md#monitor-the-backup-jobs)
uses its own cursor for the two backup units and includes a sanitized archive
freshness snapshot on every poll.

#### Watch the check runner from another host

Install the same journal reader on the check runner. From the independent
monitoring host, invoke it once per minute for each installed check over the
approved SSH route. Use pinned host keys, a bounded SSH deadline and an
account whose command is restricted to the expected read-only checks.
Establish a first completed run for each timer before arming its watcher.

```sh
timeout --kill-after=2s 30s ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o ConnectTimeout=5 sig-check-runner \
  python3 /opt/oriso/signoz-uptime/signoz_check_journal.py --check-cadence uptime
```

Use `--check-cadence heartbeat` against the runner that hosts the heartbeat
check timer. Alert separately for each check. Exit 0 means its timer is
active and has an attributable completion younger than 180 seconds on this
boot. The three-minute window allows for the one-minute schedule and probe
runtime. The response gives the age in monotonic seconds and the last probe
state. Use earlier completion pages for the probe's own incident and recovery.

Exit 2 means the timer is inactive or the current boot has no completion
inside the window. Exit 3 means the runner state or latest completion could
not be verified. Treat an SSH failure, outer timeout or missing or invalid
JSON as a separate unreachable-runner incident. Recover each incident only
after a fresh cadence result succeeds. [systemd journal timestamps](https://www.freedesktop.org/software/systemd/man/latest/systemd.journal-fields.html)

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
release. See [Dev ingress HTTP/2 mitigation](dev-ingress-http2.md) before
reinstalling the controller ConfigMap.
`global.observability.externalIngressNginxNamespace: ingress-nginx` (Dev)
makes the cluster collector scrape its pod IPs on port 10254. This
setting requires `signozCollector.enabled=true`. It works with general
application metrics disabled. The chart
cannot switch metrics on in the controller: it must run with
`--enable-metrics=true` (Dev does since 2026-11-06). The collector's cluster
ClusterRole already allows `list`/`watch` on pods in all namespaces.

After the rollout, in SigNoz open Metrics and search `nginx_ingress_controller_requests`;
data from the last 5 minutes must appear. If it does not, check the controller
args (`kubectl -n ingress-nginx get deploy ingress-nginx-controller -o yaml | grep enable-metrics`)
and the collector log for `ingress-nginx` scrape errors.

## Forward Dev alerts to Buzz and OpenClaw

Keep `oriso-alerts-slack` as the Slack channel. Confirm the first delivered
Slack alert and the approved private route from Dev to the Dreambau forwarder
before activating the second channel. Verify the forwarder's dedicated Buzz
and OpenClaw identities. [Slack delivery #468](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/468) · [Dreambau forwarder](https://github.com/Storypapst/dreambau/pull/223)

With forwarding off, append a read-only Secret mount to the existing SigNoz
application container volumes. Mount the approved webhook credential as
`/var/run/oriso-signoz-agent/token` and verify it is readable in the
container. Use `signoz.signoz.additionalVolumes` and
`signoz.signoz.additionalVolumeMounts` in the reviewed Dev override.
Create the existing-channel dependency `oriso-alerts-agents` in SigNoz
with the approved private route and file-backed credential:

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

Allow the Alertmanager configuration sync after changing the channel.
SigNoz v0.135.1 uses a one-minute polling interval. [Pinned sync configuration](https://github.com/SigNoz/signoz/blob/v0.135.1/pkg/alertmanager/config.go#L38-L44)

Set `signoz.orisoObservability.agentForwarding=true` in the approved Dev
override and run the managed provisioning Job. The provisioner verifies the
Slack and webhook channel types before writing rules. The six core catalog
rules use both channels and include the fixed rule slug and unitless value.
The forwarder configuration maps the slugs to these services.

| Rule | Service |
| --- | --- |
| `provisioning-compensation-failure` | `userservice` |
| `availability-store-failure` | `userservice` |
| `notification-failure` | `userservice` |
| `unencrypted-room` | `userservice` |
| `clickhouse-capacity` | `clickhouse` |
| `collector-staleness` | `collector` |

Read back the rules with the existing Dev key and
`--mode check --skip-telemetry --agent-forwarding`. For the approved live
test, select a rule with a real numeric sample and use
`--mode test-rule --rule <slug> --agent-forwarding`. The evaluation count
confirms the query produced an alert. Verify the Slack, Buzz and OpenClaw
receipts and check that the latter two contain the fixed rule, service and
value. The forwarder handles measured firing values. It leaves No Data and
resolved delivery to the configured SigNoz channels.

To restore Slack-only routing, set `agentForwarding=false` and run the
managed Job again. Read back the same six rule IDs with Slack routes and
the forwarding fields removed before retiring the private webhook and
credential mount.

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

For an approved expansion, put the agreed `signoz.clickhouse.persistence.size`
in the reviewed Dev overlay input. Include that file in `observability_inputs`
both when generating `observability_file` and when running the preflight. This
keeps the size change within the approved observability paths checked by
`values_drift`. The preflight compares the rendered ClickHouseInstallation
with the request and reported capacity of each existing claim. A provider may
report more capacity than requested. Confirm the desired size with the
operator if the planned request falls below the reported capacity.

Confirm the StorageClass supports expansion and record the current PVC and
filesystem capacity before a size change. Record the PVC UID, bound PV name
and reclaim policy, and the StatefulSet and Pod UIDs. The preflight also
compares the size against the live StatefulSet's data claim template. Growing
that template requires the StatefulSet to retain data claims both when it
scales down and when it is deleted. Kubernetes defaults either unset field
to `Retain`.

Plan the expansion for an approved maintenance window. Altinity 0.21.2 falls
back to recreating the StatefulSet when the Kubernetes update fails on an
immutable claim template. It scales the old StatefulSet to zero first.
This interrupts the single-replica Dev ClickHouse installation.
[Operator update and recreation](https://github.com/Altinity/clickhouse-operator/blob/0.21.2/pkg/controller/chi/worker.go#L1463-L1491)
· [StatefulSet claim retention](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/#persistentvolumeclaim-retention)

After the operator and CSI reconcile, require the same PVC UID and PV binding,
the approved size in both the claim request and reported capacity, and a Ready
ClickHouse Pod on the retained claim. Check the mounted filesystem and
ClickHouse's unreserved writable space before running the SigNoz signal
acceptance checks. Server dry-run admission is a preflight checkpoint.

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
helm history caritas -n caritas
verify_signoz_rollback() (
  set +x
  set -euo pipefail
  manifest=$(helm get manifest caritas -n caritas --revision "${revision:?Set the approved target revision}")
  printf '%s' "$manifest" |
    python3 scripts/check_helm_workload_images.py --namespace caritas --release caritas
  printf '%s' "$manifest" |
    python3 -m scripts.check_signoz_clickhouse_storage --namespace caritas --release caritas
)
verify_signoz_rollback
```

An image or storage mismatch requires the forward upgrade with the current
image pins and capacity settings. Apply both checks to the target of any
automatic rollback before enabling it. Leave automatic rollback off for an
expansion if the saved revision requests less than the planned new capacity.
Review the rollback's resource removals and hook Jobs separately, and
require it to retain the SigNoz, ClickHouse, ZooKeeper and backup claims.

## E2E trace ingest (Dev nightly)

The scheduled GitHub run (`nightly-dev-e2e`, ORISO-E2E#97) sends spans to
`https://dev.oriso.org/signoz-e2e/v1/traces`. The chart enables it with
`values-dev.yaml` and requires the `dev` environment and exact Dev hostname.
Use both `global.observability.e2eTraceIngest.enabled` and
`signoz.otelCollector.ports.otlp-e2e.enabled` for that release.

The collector Service stays on `ClusterIP` with the vendored ingress off.
ingress-nginx requires the exact request URI and HTTP Basic credentials
(user `e2e`) from an existing Secret. It forwards small uncompressed POSTs
at up to five requests per second to the dedicated TCP receiver on port
4321. Both the ingress and receiver cap the body at 256 KiB.

The dedicated trace pipeline keeps spans with resource
`service.name=oriso-e2e`, stamps `deployment.environment=dev` from the
collector environment and adds `oriso.signal.source=e2e`. The chart
references the existing Secret by name.

The repo admin does this once, before the rollout that enables the route (steps only;
generate the token yourself and do not paste it into a ticket or chat):

1. Generate a long random token in your password manager.
2. On the Dev master (`KUBECONFIG=/etc/rancher/k3s/k3s.yaml`), create the Secret
   `signoz-e2e-ingest-auth` in namespace `caritas` with one key `auth` holding an
   htpasswd line for user `e2e`. Run `htpasswd -nB e2e` and enter the token at
   its prompt, so the token never appears on a command line or in shell history.
3. In GitHub, repository ORISO-E2E, environment `dev-e2e`, add the secrets
   `DEV_OTLP_TOKEN` (the token) and `DEV_OTLP_ENDPOINT`
   (`https://dev.oriso.org/signoz-e2e/v1/traces`). The same environment also needs
   `ORISO_TEST_ACCESS_TOKEN` and `TEST_ACCESS_IDENTITY` (as in `predev-e2e`).
4. Roll out with the observability overlay (Rollout above), then run the workflow once
   by hand (`workflow_dispatch`).

Verify HTTP 401 for an anonymous POST to the exact URI and rejection of GET
and URI variants. With the scoped token, the Nightly report links stored
`oriso-e2e` spans under `oriso.signal.source=e2e`. Verify the dedicated
pipeline drops a synthetic span with another service name. Rotate the Secret
and `DEV_OTLP_TOKEN` together.

To disable the route, set both the route and dedicated Service-port flags to
`false`. The chart removes the Ingress, Service port and dedicated receiver
and pipeline. The updated collector configuration rolls its pods.
