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

## Vendored chart policy

`charts/k8s-infra` is a byte-identical copy of the official SigNoz `k8s-infra`
chart `0.17.0`, including its `tests/` suites, which assert *upstream* defaults
only. Do not edit anything under `charts/k8s-infra`: report defects upstream and
re-vendor, so a version bump never silently discards a local patch. Verify with:

```bash
helm repo add signoz https://charts.signoz.io
helm pull signoz/k8s-infra --version 0.17.0 --untar --untardir /tmp/upstream
diff -r charts/k8s-infra /tmp/upstream/k8s-infra
```

Every ORISO-specific guarantee — namespace-scoped log tailing, the
`transform/oriso_log_privacy` processor, the `otlphttp` exporter, least-privilege
RBAC, and the collector self-telemetry identity — is applied by the parent chart
and is therefore *not* covered by the vendored suites. `tests/render_signoz_k8s_infra_test.py`
is the only gate that proves that contract; keep it in the required CI set.

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

If the infrastructure collectors overload the node or reject configuration,
set `k8s-infra.enabled=false` in the affected environment overlay and re-apply
the chart. This disables infrastructure collection without removing the SigNoz
backend or its retained ClickHouse volume. Capture collector logs and the failed
acceptance JSON before rollback when safe to do so.

This is a human-approved deployment step, not a CI step. Run it from the chart
root of the reviewed revision, with the overlay of the affected environment:

```bash
# PreDev; use values-dev.yaml and the Dev release/namespace for Dev.
helm upgrade --install caritas ./ -n caritas --create-namespace \
  --wait-for-jobs --timeout 15m \
  -f values.yaml -f values-pre-dev.yaml -f secrets.yaml \
  --set k8s-infra.enabled=false
```

To go back to the previously deployed revision instead of re-rendering:

```bash
helm history caritas -n caritas
helm rollback caritas <REVISION> -n caritas --wait-for-jobs --timeout 15m
```
