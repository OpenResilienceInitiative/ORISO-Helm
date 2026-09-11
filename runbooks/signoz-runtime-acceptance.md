# SigNoz runtime acceptance

Use this gate after every SigNoz chart upgrade in PreDev and again after the
reviewed promotion to Dev. Kubernetes readiness alone is not acceptance: the
gate must prove that OTLP traces, metrics, and logs reach ClickHouse.

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
  --environment predev \
  --ssh-host root@<predev-host>
```

Repeat after the reviewed Dev promotion with `--environment dev` and the Dev
host. The command prints a JSON result that can be attached to the PR or release
evidence. A successful result contains positive counts for `traces`, `metrics`,
and `logs` under `syntheticReadback`.

The gate creates short-lived `curlimages/curl` probe pods with `--rm`; Kubernetes
removes each pod when its OTLP request completes. It does not persist or print
ClickHouse credentials.

## Readiness-only diagnosis

For a non-ingesting diagnostic pass:

```bash
python3 scripts/signoz_runtime_acceptance.py \
  --namespace caritas \
  --release caritas \
  --environment predev \
  --ssh-host root@<predev-host> \
  --skip-synthetic
```

This weaker mode is useful while diagnosing a rollout, but it is not sufficient
release evidence.
