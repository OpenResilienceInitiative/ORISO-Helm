# SigNoz runtime acceptance

Use this gate after every SigNoz chart upgrade in PreDev and again after the
reviewed promotion to Dev. Kubernetes readiness alone is not acceptance: the
gate must prove that OTLP traces, metrics, logs, Kubernetes infrastructure
signals, and collector self-telemetry reach ClickHouse.

This is the ingestion half of the release proof. The managed dashboards,
alerts, and Slack route are provisioned and proved by
`runbooks/signoz-managed-observability.md`.

## What the gate proves

The command fails closed unless all of the following are true:

- the ClickHouseInstallation exists and is not being deleted;
- the ClickHouse operator, ClickHouse, SigNoz, and OTEL Collector rollouts are
  ready;
- the telemetry-store migration job completed;
- the live collector configuration has OTLP HTTP and gRPC receivers plus a
  ClickHouse exporter in each trace, metric, and log pipeline;
- every non-terminating ClickHouse data pod for the release is ready and has at
  least 2 GiB of unreserved space on its default data disk before any probes run.
- a privacy-safe synthetic trace, metric, and correlated log can be sent to the
  in-cluster collector and read back from the corresponding ClickHouse tables.
- the node-local and cluster-wide `k8s-infra` collectors are ready;
- pod, node, node-condition, and host metrics carry the intended environment
  and cluster identity;
- collector self-metrics are present, so a silent collector failure is visible;
- a synthetic Kubernetes Event is collected;
- a representative nested ORISO JSON log is collected with trace correlation,
  while its free-text body and stack marker are provably absent.
- all four Java services have fresh stored logs, metrics and traces with their
  canonical service name and the expected deployment environment. Every fresh
  span has a populated service version. Each service also has a stored log
  whose trace ID and span ID match a span in that scope.

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
that can be attached to the PR or release evidence. A successful result sets
`clickhouseCapacity` to `passed` and records positive trace, metric, and log
counts under `syntheticReadback`. It also records positive infrastructure counts
under `k8sInfraReadback` and exactly zero for `forbiddenLogBody`.
`javaSignals` records the stored Java telemetry evidence for the full gate.

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

Use this mode for rollout and pipeline diagnosis. It reports capacity and
persistence as skipped. Release evidence requires the full command.

Use `--skip-k8s-infra` only when validating the backend-only repair before the
stacked Kubernetes collection change is installed. Full PreDev and Dev evidence
also requires infrastructure readiness, infrastructure readback and stored Java
signal verification.

## Read stored Java signals

Use the read-only mode to inspect the same stored Java signal contract on its
own. Set `DEV_SIGNOZ_SSH_HOST` to the intended Dev SSH host.

```bash
python3 scripts/signoz_runtime_acceptance.py \
  --namespace caritas --release caritas --environment dev \
  --ssh-host "$DEV_SIGNOZ_SSH_HOST" --check-java-signals
```

The report counts actual log, metric and trace samples within 15 minutes for
`user-service`, `agency-service`, `tenant-service` and `consulting-type-service`.
For each service, every fresh trace row must carry the exact
`deployment.environment` and a populated `service.version`. Missing, blank and
`unknown` versions count as invalid. The report includes the fresh trace total
and separate counts for wrong environments and invalid versions. Multiple
populated versions can coexist during a rollout.

The 60-minute correlation requires matching trace and span IDs from the same
service and deployment environment. The command exits 1 on missing signals or
trace identity violations and returns aggregate counts. For release evidence,
run the full command once the fresh window reflects the reviewed rollout.
