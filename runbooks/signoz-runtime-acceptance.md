# SigNoz runtime acceptance

Use this gate after every SigNoz chart upgrade in PreDev and again after the
reviewed promotion to Dev. Kubernetes readiness alone is not acceptance: the
gate must prove that OTLP traces, metrics, logs, Kubernetes infrastructure
signals, and collector self-telemetry reach ClickHouse.

This is the ingestion half of the release proof. The managed dashboards,
alerts, query execution, environment separation, and Slack route are proved by
`runbooks/signoz-managed-observability.md`; both gates are mandatory.

## What the gate proves

The command fails closed unless all of the following are true:

- the ClickHouseInstallation exists and is not being deleted;
- the ClickHouse operator, ClickHouse, SigNoz, and OTEL Collector rollouts are
  ready;
- the telemetry-store migration job completed;
- the live collector configuration has OTLP HTTP and gRPC receivers plus a
  ClickHouse exporter in each trace, metric, and log pipeline;
- a privacy-safe synthetic trace, metric, and correlated log can be sent to the
  in-cluster collector and read back from the corresponding ClickHouse tables.
- the node-local and cluster-wide `k8s-infra` collectors are ready;
- pod, node, node-condition, and host metrics carry the intended environment
  and cluster identity;
- collector self-metrics are present, so a silent collector failure is visible;
- a synthetic Kubernetes Event is collected;
- a representative nested ORISO JSON log is collected with trace correlation,
  while its free-text body and stack marker are provably absent.

The active probe contains only the service name `oriso-signoz-acceptance`, the
deployment environment, and a random acceptance ID. It never contains user
identifiers, email addresses, tokens, message bodies, or application data.

## Run remotely

Run from a trusted operator checkout with SSH and `kubectl` access to the
target server:

```bash
python3 scripts/signoz_runtime_acceptance.py \
  --namespace caritas \
  --release caritas \
  --environment pre-dev \
  --cluster-name oriso-predev \
  --ssh-host root@<predev-host>
```

Repeat after the reviewed Dev promotion with `--environment dev`,
`--cluster-name oriso-dev`, and the Dev host. The command prints a JSON result
that can be attached to the PR or release evidence. A successful result contains
positive counts for `traces`, `metrics`, and `logs` under `syntheticReadback`,
positive infrastructure signal counts under `k8sInfraReadback`, and exactly
zero for `forbiddenLogBody`.

The gate creates short-lived `curlimages/curl` probe pods with `--rm`, one
short-lived BusyBox log probe, and one namespaced synthetic Kubernetes Event.
It removes the log probe after successful or failed readback and does not
persist or print ClickHouse credentials. The Event contains only the random
acceptance ID and expires under the cluster's normal Event retention policy.

## Readiness-only diagnosis

For a non-ingesting diagnostic pass:

```bash
python3 scripts/signoz_runtime_acceptance.py \
  --namespace caritas \
  --release caritas \
  --environment pre-dev \
  --cluster-name oriso-predev \
  --ssh-host root@<predev-host> \
  --skip-synthetic
```

This weaker mode is useful while diagnosing a rollout, but it is not sufficient
release evidence.

Use `--skip-k8s-infra` only when validating the backend-only repair before the
stacked Kubernetes collection change is installed. That mode omits both
infrastructure collector readiness and infrastructure readback, so it is also
not sufficient as final PreDev or Dev evidence.
