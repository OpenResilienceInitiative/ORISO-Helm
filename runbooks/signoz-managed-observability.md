# ORISO-managed SigNoz observability

This runbook provisions and proves the ORISO dashboards and alert rules for
SigNoz `v0.135.1`. It is separate from the active OTLP/ClickHouse acceptance in
`runbooks/signoz-runtime-acceptance.md`: both gates must pass.

## What the release owns

The post-install/post-upgrade Job `caritas-signoz-observability` idempotently
creates or updates:

- display title `ORISO Live Chat and Matrix [<environment>]`, identity
  `oriso-live-chat-<environment>`: demand, routing, Redis availability, room
  encryption, Matrix processing, push, and notification outcomes;
- display title `ORISO Service Reliability [<environment>]`, identity
  `oriso-service-reliability-<environment>`: provisioning compensation,
  dependency calls, retries, and replica-safety constraints;
- display title `ORISO Kubernetes and SigNoz Health [<environment>]`, identity
  `oriso-platform-health-<environment>`: collector freshness, pod/node health,
  and ClickHouse PVC capacity;
- six active alert rules named `ORISO <environment> | ...` for incomplete
  compensation, Redis failure, notification failure, unencrypted rooms, low
  ClickHouse capacity, and stale collector telemetry. SigNoz generates one
  route policy per rule when the rule is saved.

### Optional: E2E test dashboards (off by default)

With `signoz.orisoObservability.e2eDashboards=true` (default `false`) the same
Job also manages the two test dashboards for ORISO-E2E#97. The Playwright
panels read `oriso-e2e` spans from `tests/support/otelReporter.ts`. The CI
panels read `oriso-ci-tests` logs from the separate native report exporter.

- `ORISO E2E Test Health [<environment>]` counts each identified first-attempt
  run once per Playwright test and project. The pass rate uses Playwright's
  expected outcome, so an expected failure counts as a pass. A run with any
  unexpected first attempt counts as unexpected. A blank test ID identifies
  the separate legacy grouping by project, file and title. The rate requires
  a recorded run ID. The other panels show stored attempt rows, durations and
  recent runs.
- `ORISO E2E Flaky Tests [<environment>]` groups passes and unexpected failures
  by the same Playwright test ID, project and full commit SHA. A timeout with a
  failed span counts as a failure. The reporter's `test.flaky` flag also has its
  own panel for passing retries. Test Health remains available for older spans.
  The same-commit table requires the stable `test.id` from ORISO-E2E#110.
- The CI pass rate counts terminal outcomes per distinct GitHub job. A job
  with a failed record counts once as failed. The CI same-commit table
  requires a passing job and a separate failing job for the same repository,
  project, test and checkout. A job with conflicting results does not count as
  a passing or failing observation in that table.
- The five seed panels match the exact source project, file and cases from
  ORISO-Frontend#1613, #1707, #1443, ORISO-UserService#251 and ORISO-Admin#1016.
  They show the earliest dated CI observation in the selected time range.
  The numeric field is the report window end in epoch milliseconds and the
  table formats it as a date using the display time zone. An empty table
  means there is no matching dated observation in the ingested results.

The Playwright reporter sets `deployment.environment` to the project name.
The runtime panels scope to that environment. The CI panels scope to the
approved repository, framework and project combinations and to hosted,
dated terminal results marked by the dedicated collector. CI report
collection and operator replay are separate from dashboard provisioning. The Dev nightly runtime gate
remains in ORISO-E2E#97 D.4.

It does **not** own the notification channel. The Slack channel
`oriso-alerts-slack` (Slack `#oriso-alerts-and-webhooks`) is made by hand in
SigNoz from a webhook that stays secret. The rules reference it by name
(`signoz.orisoObservability.channelName`); the program never creates, updates,
or tests a channel and never needs the webhook. If the channel is missing, the
hook stops before writing anything.

Every query has a fixed `deployment.environment` filter; infrastructure queries
also have a fixed `k8s.cluster.name` filter. Dashboard variables are absent,
alert grouping is limited to the static `environment` label, and the program
rejects assets that mention user, room, IP, token, or message-content fields.

### Verify a Dev CI replay

After the approved operator replay and Collector batch flush, pass the same
sanitized exporter payload files to the read-only verifier. Use the existing
Dev API key through the private `SIGNOZ_API_KEY` environment.

```bash
python3 files/signoz/verify_ci_replay.py storybook.otlp.json unit.otlp.json admin.otlp.json userservice.otlp.json
```

The verifier reads up to sixteen payload files and queries each job inside its
recorded UTC evidence window. The bounded raw query selects only the CI
fields and preserves the stored Float64 duration and nanosecond timestamps.
The verifier checks the collector marker, scope, identity, outcome, retry
and optional duration. It reports aggregate counts for exact,
missing, duplicate, conflicting and unexpected rows. Warnings, cursors and
invalid responses make the read inconclusive. A clean result needs exactly
one stored row per expected test and both stored CI dashboard specifications
to match the catalog.

For a partial delivery or ambiguous request result, run the readback before
considering a retry. Compare the saved dashboard panels in the replay window
separately from the row and query-definition check.


## Switch and what it costs

Everything here is behind `signoz.orisoObservability.enabled`, default `false`
in `values.yaml.default`. The Dev overlay already enables it and names the
`signoz-observability-api-key` Secret. Pre-Dev explicitly disables it. Enabling it
requires `signoz.enabled=true` and `signozCollector.enabled=true`
(`templates/observability-validation.yaml` fails the render otherwise).

When it is on, the Job is a Helm post-install/post-upgrade hook. A failing hook
fails the release, so a broken API key or an unreachable SigNoz blocks that
deploy. On a first Dev install, explicitly override the Dev overlay to keep the
hook off until the API key Secret exists. The telemetry-dependent proof has its
own switch:

| Value | Default | Effect |
|---|---|---|
| `signoz.orisoObservability.enabled` | `false` | Render the ConfigMap and the hook Job. |
| `signoz.orisoObservability.conformanceCheck` | `false` | Require fresh grouped data on the three core dashboards. Validate the optional E2E query responses. Execute the alert queries and verify collector freshness and separation. |
| `signoz.orisoObservability.channelName` | `oriso-alerts-slack` | Existing channel the rules route to. |
| `signoz.orisoObservability.apiKeySecret.name` / `.key` | `""` / `apiKey` | Existing Secret with the SigNoz API key; the name is required when on. |

With `conformanceCheck` off, a hook run upserts dashboards and rules and reads
every dashboard, rule, the channel, and the routes back against the catalog.
Enabling the optional Dev platform rules also requires their telemetry before
any writes. Turn `conformanceCheck` on only after the
UserService diagnostic metrics and the collectors are live in that environment.
Exercise the approved scenarios for event-driven panels before running the full
check. Each core dashboard query needs a complete interval with a finite sample
in the last 15 minutes. Every contributing series must have the panel's declared
group keys. A numeric zero is valid. The optional E2E dashboards accept valid
empty or sparse query results and legacy rows with a blank test ID. Verify their
contents after a known test run as part of ORISO-E2E#97.

## Dev platform alerts

`signoz.orisoObservability.platformAlerts.enabled` defaults to `false`. Enable it
on Dev after the Kubernetes collector, ingress-nginx app scrape, and data store
metrics are reporting. The chart requires both `global.observability.appMetrics`
and `global.observability.dbMetrics` to enable these rules. It also requires
`global.observability.externalCertManagerNamespace` for certificate discovery.
Each rule must return a current result, including a valid zero, for
every eligible object observed by its metrics. The provisioner compares both
queries at the same completed minute before any writes. Compare the reporting
population with the cluster inventory during the installed acceptance check.

| Rule | Trigger | Severity |
| --- | --- | --- |
| Node NotReady | `Ready=False` or `Ready=Unknown` in four consecutive populated 30-second intervals. | Critical |
| Critical Deployment unavailable | Available replicas below desired in ten consecutive 30-second intervals for `ingress-nginx-controller`, `ip-anonymizer`, `keycloak`, `userservice`, `matrix-synapse`, or `frontend`. | Critical |
| Deployment unavailable | Same replica check for other Deployments. | Warning |
| StatefulSet unready | Ready replicas below desired in ten consecutive 30-second intervals. | Warning |
| Container CrashLoopBackOff | A current `CrashLoopBackOff` condition in the last 30 seconds. | Critical |
| Container restart count increase | The observed restart count rises by more than three in 15 minutes for the same Pod UID and container. | Critical |
| IP anonymizer unavailable | Fewer than one available replica in two populated 30-second intervals for `caritas/ip-anonymizer`. | Critical |
| Ingress 5xx rate | Strictly more than 2% of observed requests over five minutes, grouped by ingress namespace and name. | Critical |
| Data store reachability | No successful observation of MariaDB, MongoDB, RabbitMQ or Redis for five minutes, with continuous collector coverage. Each service has its own rule. | Critical |
| Keycloak login failures | More than five times the usual failed-login count over ten minutes. The reference is the previous hour, excluding the evaluated ten minutes. | Warning |
| TLS certificate expiry | Less than 21 days until expiry, including certificates whose expiry has already passed, grouped by Certificate namespace and name. | Warning |
| PVC capacity | Strictly above 75%, 85%, or 95% on its fullest reporting mount. Only the highest applicable tier matches. | Info / Warning / Critical |
| Node filesystem capacity | Strictly above 85% using the kubelet's node filesystem measurements. | Warning |

Replica checks pair observations for the same resource identity before grouping
by name. A complete scaled-to-zero or recovered workload contributes zero.
A missing interval or overlapping streams under a reused name leave the result
unknown. CrashLoopBackOff reports the current Kubernetes condition. Its
evaluation and readiness check exclude a Pod when one phase stream identifies
its UID as Succeeded or Failed within five minutes. A missing or ambiguous
phase leaves the container in scope. The restart rule joins samples by stable
Pod UID and container name through changes of
runtime container ID. It evaluates containers sampled at both ends of the
15-minute window. Missing intervals, duplicate streams and observed resets of
the Kubernetes restart count leave the result unknown.

The ingress rule checks the controller's request counter per series before
combining statuses and controller replicas. A sampled route with no 5xx
series has zero observed errors. A route with unchanged, covered counters
has zero observed requests. Missing or incomplete counter samples leave the
result unknown.

The authenticated data store receivers add a source marker and emit instance
uptime metrics, including RabbitMQ nodes with no queues. Redis uses a successful
`redis_up=1` observation from its annotated exporter. During provisioning, each
store needs a successful sample within 150 seconds. At runtime, the same
collector process must report in six consecutive one-minute intervals before
the rules can infer a five-minute absence. During a collector gap or restart,
the collector-staleness rule covers the shared failure. To investigate a Redis
alarm, compare Prometheus scrape `up` with `redis_up` to distinguish the exporter
path from the backend. The other store alarms call for checking the service and
its monitoring-user access.

The Keycloak rule combines observed counter increases across the Dev instances
and realms. It compares whole counts from successive one-minute samples.
It uses the app scrape on
`/auth/metrics` and the `keycloak_user_events_total` login series. Successful
scrapes in both windows and a recent login event series establish coverage.
Allow at least 70 minutes of scrape history before enabling the rule. The
reference count per ten minutes has a floor of one, so with a quiet
reference the alert starts at six observed failures. A newly observed error
counter can contribute its initial value when its scrape target was already
healthy before the window. The alert carries only the cluster and environment.

The Dev overlay discovers the cert-manager controller pods on their
`http-metrics` port. The existing pod discovery permission covers the
controller namespace. The collector retains the expiration gauge for
Certificates in the release namespace and the scrape's own `up` series.
The rule needs a recent successful scrape from each reporting instance and
agreement on the issuer and expiry. It treats a zero timestamp, unavailable
scrape and conflicting issuer streams as unknown. Compare the reported
Certificate names with the Dev inventory during installed acceptance. The
external S7 probe checks the TLS certificate actually served by the ingress.

Storage rules compute the share of capacity unavailable to workloads from
the kubelet's available bytes and total capacity. The node filesystem figure
includes space the filesystem reserves. The PVC query pairs the capacity and
available measurements for each mount before grouping by namespace and claim.
Negative available bytes, zero capacity, or an unmatched measurement leave the
result unknown. Alerts retain only environment, cluster, and the applicable
Kubernetes names. The collector-staleness rule covers loss of collector data.

Use `--platform-alerts` for a workstation check of an enabled installation.
To turn it off, set the value to `false` while managed observability still runs.
The next hook disables all existing ORISO-managed platform rules in place and
retains their IDs. This path works with missing platform telemetry.
See [Helm #542](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/542)
for the remaining platform alerts and external monitoring.

## Dev heartbeat

`signoz.orisoObservability.heartbeat.enabled` defaults to `false`. Before
enabling it in Dev, provision a receiver outside the monitored cluster and
configure its incident destination. In SigNoz, create the dedicated channel
named by `signoz.orisoObservability.heartbeat.channelName` (default
`oriso-dev-heartbeat`). Use one HTTPS webhook with certificate verification,
Basic Auth and resolved delivery disabled. Keep its credentials in SigNoz
and the receiver's secret store. The Helm hook checks the channel and fresh
collector telemetry before writing assets.

The rule `ORISO dev | SigNoz heartbeat` emits its evaluation timestamp in
`oriso_evaluated_at`. It returns a timestamp while the named Dev cluster
collector has supplied a sample within 150 seconds. The rule evaluates every
minute and requests firing-only repeat notifications every minute.
The pinned SigNoz Alertmanager groups subsequent deliveries at five-minute
intervals. Measure the interval in the receiver before enabling incidents.

Require authenticated callbacks for this rule and exact cluster and environment
labels. Accept a single firing alert with a recent, strictly increasing
`oriso_evaluated_at` timestamp in Unix seconds. Reject duplicate, stale, foreign
and resolved messages without resetting the deadline. Set the watchdog to
raise an incident after 600 seconds without an accepted pulse. Measure this
from the last accepted receipt. Qualify the receiver with an approved outage
and recovery test before relying on that deadline.

Use `--heartbeat --heartbeat-channel-name <name>` with the workstation check to
verify the stored rule, receiver configuration and current collector sample.
To stop the heartbeat, put the external check in maintenance and set
`heartbeat.enabled=false` while managed observability still runs. Keep the
configured channel name for that upgrade. The hook disables the existing rule
in place and retains its ID even if the telemetry is unavailable.

## External Dev uptime and TLS

From a runner outside the Dev cluster, verify the certificate for
`signoz.oriso.org` and expect its HTTPS root to redirect to
`https://dev.oriso.org/signoz/` with a successful final response. Warn when
the certificate served by the alias has less than 21 days remaining.
Check `https://dev.oriso.org/signoz/api/v1/health?live=1` independently for
HTTP 200 and JSON with `"status": "ok"`. This is the SigNoz application
liveness endpoint. The collector and stored-signals path is covered by the
separate Dev heartbeat. Keep both checks and their incident delivery outside
the monitored cluster.

## API key Secret (created out of band, never in values)

```text
Secret <signoz.orisoObservability.apiKeySecret.name>   key: apiKey
```

An Editor-role SigNoz API key is enough: in `v0.135.1` dashboard and rule
writes and `POST /api/v1/testRule` need Editor, and every read the check uses
(dashboards, rules, channels, route policies, `query_range`) needs Viewer.
Never put the key in Helm values, a GitHub comment, terminal output, or this
repository. Verify key names without reading values:

```bash
kubectl -n caritas get secret <api-key-secret> \
  -o go-template='{{range $key, $value := .data}}{{$key}}{{"\n"}}{{end}}'
```

### After a SigNoz reinstall

The API key and the hand-made channel live in SigNoz's own store. A reinstall
that wipes that store removes both, so the next upgrade fails the hook until
someone recreates the `oriso-alerts-slack` channel and a new API key (written
into the same Secret). Re-run `helm upgrade` afterwards; the hook then
recreates every dashboard and rule with no further clicks.

## First installation in an environment

`<environment overlay>` is the whole overlay chain: `-f values-dev.yaml` for Dev,
`-f values-dev.yaml -f values-pre-dev.yaml` for Pre-Dev (Pre-Dev loads after Dev;
the Pre-Dev overlay alone leaves SigNoz and the collector off).

1. Deploy SigNoz and the collectors with provisioning explicitly disabled.
   The Dev overlay turns it on, so put the override **after** the overlay:

   ```bash
   helm upgrade --install caritas . \
     --namespace caritas --create-namespace \
     -f values.yaml -f secrets.yaml <environment overlay> \
     --set signoz.orisoObservability.enabled=false \
     --wait-for-jobs --timeout 15m
   ```

   Run the active ingestion gate from `runbooks/signoz-runtime-acceptance.md`.
2. Only for an environment approved for managed observability, create the
   `oriso-alerts-slack` Slack channel in SigNoz and an Editor-role
   API key; store the key under `apiKey` in a Secret in the release namespace.
   For Dev, use the name `signoz-observability-api-key` from the overlay.
3. For an approved environment, run the read-only check below from a workstation.
   The example commands target Dev; for another approved environment use its own
   SigNoz URL, deployment environment and collector cluster name. Missing
   dashboards are expected before the first provisioning.
4. For Dev, upgrade with the overlay and without the temporary `--set` override;
   it already enables provisioning and names the Secret. For another environment,
   enable provisioning only if it is approved for that environment: set
   `signoz.orisoObservability.enabled=true` and
   `signoz.orisoObservability.apiKeySecret.name` in that environment's values
   before upgrading. Keep Pre-Dev and every other disabled environment at
   `signoz.orisoObservability.enabled=false`; skip the channel, API key and
   provisioning check there.

```bash
helm upgrade --install caritas . \
  --namespace caritas --create-namespace \
  -f values.yaml -f secrets.yaml <environment overlay> \
  --wait-for-jobs --timeout 15m
```

`values.yaml` is the local copy of `values.yaml.default` with
`global.domainName`, `global.keycloak.realm`, and the Matrix server-name fields
set for the target environment; keep it out of Git. The timeout matches the
Job's `activeDeadlineSeconds: 900`.

Read the retained evidence:

```bash
kubectl -n caritas logs job/caritas-signoz-observability
```

The final line must start with one of:

```text
PASS: SigNoz dashboards, alerts, channel, and routes stored as catalogued for <environment> (telemetry not checked)
PASS: SigNoz dashboards, alerts, routes, queries, freshness, and environment separation for <environment>
```

The second line appears only with `conformanceCheck=true`.

## Upgrading an existing Dev release

Before the upgrade, confirm that `oriso-alerts-slack` exists in Dev SigNoz
and that `signoz-observability-api-key` in the release namespace contains
`apiKey`. Use the [Secret key-name check](#api-key-secret-created-out-of-band-never-in-values).
If either prerequisite is missing, stage it through the approved setup or
append `--set signoz.orisoObservability.enabled=false` after the Dev overlay
for this upgrade. Remove the override after both prerequisites are present.

## Workstation commands

The commands below are **Dev examples**. For another approved environment,
replace the URL, `--environment`, and `--cluster-name` with that target’s
configured values before running them. Do not run provisioning checks for a
disabled environment.

The script runs from a chart checkout with the Python standard library only.
The key is read from the environment, never from the command line, and is
never printed. Pass the SigNoz base URL **including the `/signoz` path** with
`--api-url` so a workstation's `SIGNOZ_API_URL` or `SIGNOZ_URL` cannot override
the target. SigNoz `v0.135.1` serves every API route under the path of
`signoz_global_external__url`, so `https://dev.oriso.org/signoz` through the
ingress, or `http://localhost:8080/signoz` through a `kubectl port-forward` of
the `caritas-signoz` service. Without the path every call returns 404.

```bash
signoz_target=https://dev.oriso.org/signoz
read -rs SIGNOZ_API_KEY && export SIGNOZ_API_KEY

# Read-only, structural: compare stored objects with the catalog, run no queries.
python3 files/signoz/signoz_observability.py --mode check \
  --api-url "$signoz_target" --environment dev --cluster-name oriso-dev --skip-telemetry

# Read-only, full: verify fresh grouped dashboard data, execute alert queries,
# and check collector freshness and environment separation.
python3 files/signoz/signoz_observability.py --mode check \
  --api-url "$signoz_target" --environment dev --cluster-name oriso-dev

# One synthetic notification for one catalog rule, no stored rule.
python3 files/signoz/signoz_observability.py --mode test-rule --rule unencrypted-room \
  --api-url "$signoz_target" --environment dev --cluster-name oriso-dev
```

`--mode check` performs no writes. It fails on missing, obsolete, or disabled
assets, panels removed from the grid, changed queries or group-by fields,
unbounded grouping, protected fields, unresolved placeholders, a missing
channel, and (without `--skip-telemetry`) failed queries, stale
collector telemetry, or cross-environment telemetry.

Rule slugs for `--mode test-rule`: `provisioning-compensation-failure`,
`availability-store-failure`, `notification-failure`, `unencrypted-room`,
`clickhouse-capacity`, `collector-staleness`.

### What the test notification does and does not prove

`--mode test-rule` posts the catalog rule, renamed with a ` [test]` suffix, to
SigNoz `POST /api/v1/testRule`. SigNoz evaluates it once and sends the result
to the rule's channel without storing a rule. The rename matters: SigNoz keys
the notification config by alert name, so posting the managed name would
overwrite the stored rule's config. The output prints `alertCount`; `0` means
the query returned no series and nothing was sent, so pick a rule whose metric
already has data (`collector-staleness` and `clickhouse-capacity` use collector
metrics).

The scheduled `collector-staleness` rule checks a five-minute rolling window
with a one-minute absence interval. Verify a missing series through a No Data
entry in the scheduled rule history and a corresponding Slack receipt.

The one-off endpoint validates the rule shape and configured channel. Record
receiver confirmation for the test notification and one scheduled dispatch
per environment in the release evidence.

## Java deployment identity alert

The optional observer and rule are disabled by default. Enable
`signoz.orisoObservability.deploymentIdentity.enabled` in a reviewed Dev or
Pre-Dev release with managed observability enabled. Keep the flag off until
the approved release record has image pins for `agencyservice`,
`consultingtypeservice`, `tenantservice`, and `userservice`. Configure
`signoz.orisoObservability.deploymentIdentity.expectedImages` with one entry
per workload. Each entry has an `image` reference pinned to `@sha256:…`
and a nonempty `runtimeDigests` list of canonical `sha256:…` digests.

Record the expected image from the approved rollout and its CI provenance
before inspecting the target. Include the canonical runtime digests verified
for the target platform. An OCI index may resolve to a different platform
manifest digest. Keep the expectation current when an approved image-only
rollout happens outside Helm. The installed Helm manifest can lag an
image-only rollout.

The CronJob runs every two minutes. Its Role can read the four Deployments
and list Pods and ReplicaSets in the release namespace. The list requests
select the four workload labels. The job checks the observed generation,
replica counts, controller UIDs, active revision, and primary container image.
It emits `oriso.deployment.identity.mismatch` with value 0 for a verified match
or 1 for a verified mismatch. An incomplete rollout, missing primary container,
or incomparable runtime ID produces `oriso.deployment.identity.unverified=1`
and a failed observation Job. The metrics carry service, environment, and
cluster only. Image digests and Pod identifiers stay out of SigNoz and Slack.

After the authorized rollout, verify fresh samples for all four services in
the target environment. Check the rule with `--mode check --deployment-identity`
and inspect the current mismatch and unverified values. The managed rule
uses the latest samples and a maximum across services in that environment and
cluster. A separate authorized route test and a confirmed Slack receipt
complete alert acceptance.

To stop the observer, turn off `deploymentIdentity.enabled` while leaving
the managed observability hook enabled for the upgrade. The hook disables
the previously owned rule and preserves the six baseline rules. If the entire
hook is disabled in the same upgrade, disable the rule in SigNoz during the
approved rollback.

## Promotion

Repeat the same gates per environment; each hook run is scoped to its own
`global.observability.deploymentEnvironment` and `signozCollector.clusterName`.
Do not call the work live until chart ancestry, deployed images, Job evidence,
active ingestion, SigNoz UI, and Slack delivery have each been verified in that
environment.

## Rollback

Re-deploy the last reviewed chart revision. If alert noise itself is the
incident, disable the six `ORISO <environment> | ...` rules in SigNoz; leave
the channel alone. Setting `signoz.orisoObservability.enabled=false` stops
future hook runs but intentionally does not delete server-side dashboards,
rules, or routes.

Do not delete the API key Secret during rollback. Rotate the key through the
normal secret-management process if it may be exposed.

## Read stored browser Web Vitals

Run the read-only check against the direct SigNoz API URL for the intended
environment. It uses the existing `SIGNOZ_URL` and `SIGNOZ_API_KEY` settings
and queries the stored browser samples independently of the managed assets.

```bash
# Record the start before the Frontend and Admin browser checks.
web_vitals_since_ms=$(python3 -c 'import time; print(time.time_ns() // 1_000_000)')

# Run after the browser activity and its metric export interval.
python3 files/signoz/signoz_observability.py --mode check-browser-vitals \
  --environment dev --since-unix-ms "$web_vitals_since_ms"
```

The report lists stored sample counts and the latest timestamp for LCP, CLS,
INP, TTFB and FCP separately for `frontend` and `admin`. The window starts at
the later of the supplied timestamp and 30 minutes ago. A CLS value of zero
is valid. The other four metrics need a positive histogram observation count.
Missing evidence produces exit code 1 and names the affected app and metric.

Periodic exports can repeat a measurement from an open tab. For the signed-in
Dev acceptance, review the browser activity and requests alongside the stored
LCP readback for both applications.

### Schedule the stored-sample check

The chart keeps the schedule off by default. Enable it in the intended
environment's final values overlay after the reviewed RUM rollout, capacity
recovery and a successful one-shot check with that environment's credential.
Reference an existing Secret with a dedicated, approved SigNoz Viewer API key
in its `apiKey` entry. The provisioning job has its own credential.

```yaml
signoz:
  orisoObservability:
    browserVitalsCheck:
      enabled: true
      apiKeySecretName: signoz-browser-vitals-reader
```

The CronJob uses the internal SigNoz API and checks the most recent 30 minutes
every five minutes. It keeps the last three successful Jobs and the last six
failed Jobs. Each run logs the aggregate evidence or the query failure.
Managed dashboard and alert provisioning has its own enable flag.

After enablement, inspect two successive scheduled Jobs for the intended
release and check each report for both applications.

```bash
kubectl -n caritas get jobs \
  -l app.kubernetes.io/name=signoz-browser-vitals,app.kubernetes.io/instance=caritas \
  --sort-by=.metadata.creationTimestamp
kubectl -n caritas logs "job/${job_name:?Set the observed Job name}" -c check-browser-vitals
```

## Production monitoring stance (proposed, ORISO-Helm#542)

Status: proposed until the production owner decides (ADR-031 in ORISO-Docs is still Proposed). The chart keeps production passive: `signoz.enabled: false`, `observability.otlpEnabled: false`, `orisoObservability.enabled: false`.

Until decided, production is covered only by the hoster's monitoring. Decide before enabling anything:

1. Owner: who is paged for production, the project team or the hoster.
2. Coverage: SigNoz in production with the platform alerts and external heartbeat, or the hoster's stack watching nodes, pods, PVCs, certificates, ingress 5xx and backups.
3. Platform alerts and heartbeat are Dev-only opt-ins (`platformAlerts`, `heartbeat`); production needs a separate review of retention, authentication, privacy boundary (#497) and recovery.
4. Backup alerts wait for the backups in #282 Task 9.
