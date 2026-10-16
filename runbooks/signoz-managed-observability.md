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

## Switch and what it costs

Everything here is behind `signoz.orisoObservability.enabled`, default `false`
in `values.yaml.default`; no environment overlay turns it on. Enabling it
requires `signoz.enabled=true` and `signozCollector.enabled=true`
(`templates/observability-validation.yaml` fails the render otherwise).

When it is on, the Job is a Helm post-install/post-upgrade hook. A failing hook
fails the release, so a broken API key or an unreachable SigNoz blocks that
deploy. That is why it stays off until the API key Secret exists, and why the
telemetry-dependent proof has its own switch:

| Value | Default | Effect |
|---|---|---|
| `signoz.orisoObservability.enabled` | `false` | Render the ConfigMap and the hook Job. |
| `signoz.orisoObservability.conformanceCheck` | `false` | Also execute every stored query, require fresh collector data, and reject data from the other environment. |
| `signoz.orisoObservability.channelName` | `oriso-alerts-slack` | Existing channel the rules route to. |
| `signoz.orisoObservability.apiKeySecret.name` / `.key` | `""` / `apiKey` | Existing Secret with the SigNoz API key; the name is required when on. |

With `conformanceCheck` off, a hook run upserts dashboards and rules and reads
every dashboard, rule, the channel, and the routes back against the catalog. It
does not need telemetry to flow yet. Turn `conformanceCheck` on only after the
UserService diagnostic metrics and the collectors are live in that environment.

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

1. Deploy SigNoz and the collectors with the switch still off. Run the active
   ingestion gate from `runbooks/signoz-runtime-acceptance.md`.
2. Create the `oriso-alerts-slack` Slack channel in SigNoz and an Editor-role
   API key; store the key in a Secret in the release namespace.
3. Run the read-only check below from a workstation (it fails on the missing
   dashboards, which is expected before the first provisioning).
4. Set `signoz.orisoObservability.enabled=true` and
   `signoz.orisoObservability.apiKeySecret.name` in the environment's values
   and upgrade:

```bash
helm upgrade --install caritas . \
  --namespace caritas --create-namespace \
  -f values.yaml -f secrets.yaml -f <environment overlay> \
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

## Workstation commands

The script runs from a chart checkout with the Python standard library only.
The key is read from the environment, never from the command line, and is
never printed. `SIGNOZ_URL` is the SigNoz base URL **including the `/signoz`
path**: SigNoz `v0.135.1` serves every API route under the path of
`signoz_global_external__url`, so `https://dev.oriso.org/signoz` through the
ingress, or `http://localhost:8080/signoz` through a `kubectl port-forward` of
the `caritas-signoz` service. Without the path every call returns 404.

```bash
export SIGNOZ_URL=https://dev.oriso.org/signoz
read -rs SIGNOZ_API_KEY && export SIGNOZ_API_KEY

# Read-only, structural: compare stored objects with the catalog, run no queries.
python3 scripts/signoz_observability.py --mode check \
  --environment dev --cluster-name oriso-dev --skip-telemetry

# Read-only, full: also execute every query, require fresh collector data,
# and reject data from the other environment.
python3 scripts/signoz_observability.py --mode check \
  --environment dev --cluster-name oriso-dev

# One synthetic notification for one catalog rule, no stored rule.
python3 scripts/signoz_observability.py --mode test-rule --rule unencrypted-room \
  --environment dev --cluster-name oriso-dev
```

`--mode check` performs no writes. It fails on missing, obsolete, or disabled
assets, panels removed from the grid, changed queries or group-by fields,
unbounded grouping, protected fields, unresolved placeholders, a missing
channel or route, and (without `--skip-telemetry`) failed queries, stale
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

It proves the rule shape, the channel name, and Slack delivery for that rule.
It does not prove that the stored rule's generated route policy dispatches;
that binding is checked structurally (`validate_managed_routes`: exactly one
`rule` route per managed rule id, carrying that id and exactly the managed
channel). Confirm one real dispatch per environment out of band, for example on
the first genuine incident, and note it in the release evidence.

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
