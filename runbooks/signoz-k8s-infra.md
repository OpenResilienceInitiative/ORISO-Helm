# SigNoz Kubernetes collection

The ORISO chart renders its own OpenTelemetry collectors (ORISO-Helm#392), in
two roles:

- a DaemonSet collects node-local host metrics, kubelet metrics, and scoped pod
  logs;
- a Deployment collects cluster metrics and namespaced Kubernetes Events.

Both export over OTLP/HTTP to the bundled SigNoz collector. They attach
`deployment.environment` and `k8s.cluster.name`, which separates PreDev
(`pre-dev`, `oriso-predev`) from Dev (`dev`, `oriso-dev`). OTLP host ports are
disabled because application SDK ingestion remains on the central collector.

## Where the configuration lives

| what | where |
| --- | --- |
| operator knobs | `signozCollector` in `values.yaml.default` (about 15 lines) |
| manifests | `templates/signoz/otel-agent-*.yaml`, `otel-cluster-*.yaml`, `otel-collector-*.yaml` |
| collector pipelines | `files/signoz/otel-agent-config.yaml`, `files/signoz/otel-cluster-config.yaml` |

These started as the rendered output of the official SigNoz `k8s-infra` chart
`0.17.0`, which the chart vendored until ORISO-Helm#392. Object names and the
`app.kubernetes.io/*` labels were kept byte-identical to that output, so the
switch is an in-place upgrade: `app.kubernetes.io/name` is part of the DaemonSet
selector and is immutable.

Upstream is no longer tracked automatically. When a Kubernetes release moves a
kubelet or metrics endpoint, port the change into `files/signoz/` by hand and
compare against the current upstream chart:

```bash
helm repo add signoz https://charts.signoz.io
helm pull signoz/k8s-infra --untar --untardir /tmp/upstream
```

Every ORISO-specific guarantee — namespace-scoped log tailing, the
`transform/oriso_log_privacy` processor, the `otlphttp` exporter, least-privilege
RBAC, and the collector self-telemetry identity — is proven by
`tests/render_signoz_k8s_infra_test.py`; keep it in the required CI set.

Bumping the collector: change `signozCollector.image.tag`, then re-run that test.

## Log privacy contract

Only logs from the release namespace are tailed. SigNoz and `kube-system` logs
are excluded. The collector parses the known nested ORISO JSON layout, retains
trace/span correlation, level, logger, and service name, then unconditionally
replaces the body with:

```text
[ORISO log body suppressed by privacy policy]
```

Free-text messages, stack traces, request correlation IDs, email addresses,
tokens, and every unlisted log attribute are not exported.

The primary containers of the four Java deployments use the same service
names for logs, traces and metrics. New agent logs carry `user-service`,
`agency-service`, `tenant-service` or `consulting-type-service` on the
resource and the log record. Earlier structured logs use `users`, `agencies`,
`tenants` or `consultingtypes`, and earlier plain logs can carry the
Kubernetes deployment name.

The transformation fails closed per record, not per batch. `error_mode` is
`ignore`, so a statement that fails on one record — truncated JSON, a
non-hexadecimal trace id — is logged and skipped, and the unconditional body
replacement and `keep_keys` still run for that record. `propagate` would return
the OTTL error to the pipeline and discard the whole batch, so one malformed
line would blind the environment.

## Release gate and rollback

Run `scripts/signoz_runtime_acceptance.py` as documented in
`runbooks/signoz-runtime-acceptance.md`. Do not promote to Dev unless every
positive signal count is non-zero and `forbiddenLogBody` is zero.

Capture collector logs and the failed acceptance JSON when safe to do so.
For an approved collector shutdown, keep the SigNoz backend and its retained
storage. Stage the collector and its dependent features off in the reviewed
environment overlay.

```bash
collector_off=(
  --set signozCollector.enabled=false
  --set global.observability.appMetrics=false
  --set global.observability.dbMetrics=false
  --set signoz.orisoObservability.enabled=false
  --set signoz.orisoObservability.deploymentIdentity.enabled=false
)
```

Append `"${collector_off[@]}"` to the Helm arguments in the
[Dev rollout preflight](signoz-dev.md#rollout) or the reviewed Pre-Dev
values layering for the affected release. For the Dev
effective-values check, use an expectation file with the same staged values.
For an approved removal of the two infrastructure collector workloads, add
`--allow-remove-infra-collectors` to both calls of `preflight_signoz` on Dev.
Use the same option on the Pre-Dev image check for that approved removal.

If the plan uses a stored Helm revision, run the
[stored revision check](signoz-dev.md#rollback) for that release and namespace.
