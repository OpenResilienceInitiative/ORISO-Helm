# SigNoz Kubernetes collection

The ORISO chart uses the official SigNoz `k8s-infra` dependency as two distinct
collectors:

- a DaemonSet collects node-local host metrics, kubelet metrics, and scoped pod
  logs;
- a Deployment collects cluster metrics and namespaced Kubernetes Events.

Both export over OTLP/HTTP to the bundled SigNoz collector. They attach
`deployment.environment` and `k8s.cluster.name`, which separates PreDev
(`pre-dev`, `oriso-predev`) from Dev (`dev`, `oriso-dev`). OTLP host ports are
disabled because application SDK ingestion remains on the central collector.

## Log privacy contract

Only logs from the release namespace are tailed. SigNoz and `kube-system` logs
are excluded. The collector parses the known nested ORISO JSON layout, retains
trace/span correlation, level, logger, and service name, then unconditionally
replaces the body with:

```text
[ORISO log body suppressed by privacy policy]
```

Free-text messages, stack traces, request correlation IDs, email addresses,
tokens, and every unlisted log attribute are not exported. Transformation
errors fail closed rather than forwarding an unprocessed body.

## Release gate and rollback

Run `scripts/signoz_runtime_acceptance.py` as documented in
`runbooks/signoz-runtime-acceptance.md`. Do not promote to Dev unless every
positive signal count is non-zero and `forbiddenLogBody` is zero.

If the infrastructure collectors overload the node or reject configuration,
set `k8s-infra.enabled=false` in the affected environment overlay and perform a
reviewed Helm rollback/upgrade. This disables infrastructure collection without
removing the SigNoz backend or its retained ClickHouse volume. Capture collector
logs and the failed acceptance JSON before rollback when safe to do so.
