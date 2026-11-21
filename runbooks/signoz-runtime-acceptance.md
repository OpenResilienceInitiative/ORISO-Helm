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
- every non-terminating ClickHouse data pod for the release is ready and meets
  the retention and disk limits below;
- immediately before sending probes, every selected data pod has at least 2 GiB
  of unreserved space on its default data disk.
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
that can be attached to the PR or release evidence. The full command records
the per-pod storage report under `clickhouseRetention` and then checks
`clickhouseCapacity`. A retention violation prints the per-pod failures and
exits before creating any probe. A successful run records positive trace,
metric, and log counts under `syntheticReadback`. It also records positive
infrastructure counts under `k8sInfraReadback` and exactly zero for
`forbiddenLogBody`.
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

Use this mode for rollout and pipeline diagnosis. It reports retention, capacity
and persistence as skipped. Release evidence requires the full command.

Use `--skip-k8s-infra` only when validating the backend-only repair before the
stacked Kubernetes collection change is installed. That scope reports retention
as skipped and still enforces the independent 2 GiB floor. Full PreDev and Dev
evidence includes retention, infrastructure readiness and readback, and stored
Java signal verification.

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

When pod exec is unavailable, run the same read-only check through the trusted
SigNoz HTTPS base URL for the environment. On Dev, use the canonical
`https://dev.oriso.org/signoz/` URL. Set `SIGNOZ_API_KEY` in the process
environment through the private credential route. The key needs access to the
SigNoz V5 ClickHouse SQL query API.

```bash
python3 scripts/signoz_runtime_acceptance.py \
  --namespace caritas --release caritas --environment dev \
  --check-java-signals --signoz-url https://dev.oriso.org/signoz/
```

For either transport, pass `--since-unix-ms` with the start of a reviewed
rollout or recovery to exclude older log, trace and metric samples. The
timestamp can shorten the usual lookback windows. The JSON report includes the
effective start of each window.

## Read ClickHouse retention and disk usage

Run the metadata check against the target environment and its SSH host.

```bash
python3 scripts/signoz_runtime_acceptance.py \
  --namespace caritas --release caritas --environment dev \
  --ssh-host "$DEV_SIGNOZ_SSH_HOST" --check-clickhouse-retention
```

The check reads each data pod's installed metadata using SELECT queries.
It applies the [ClickHouse conformance limits](https://github.com/OpenResilienceInitiative/ORISO-Helm/blob/d6de74b485410709d6d8885ee58ec57550bbe4c1/observability/signoz-conformance/contract.yaml#L77-L88).

- The canonical tables need an unconditional three-day deletion TTL on
  `event_date`. The check covers `query_log`, `text_log`, `trace_log`,
  `metric_log`, `asynchronous_metric_log`, `part_log`, `zookeeper_log`,
  `aggregated_zookeeper_log` and `processors_profile_log`.
- A verified `disabled` state exempts the canonical `metric_log` or `trace_log`
  from the TTL check. The writer probe uses the installed Linux x86_64
  ClickHouse 25.12.5.44 build, checks the current process before and after
  reading metadata, and requires stable startup configuration and no live
  collector for a removed writer. After a config reload or when evidence is
  incomplete, the report shows `unverified` and the canonical TTL is required.
- All nine log families can use up to 10 GiB in active parts. That total
  includes numbered rotations such as `trace_log_0` and tables from disabled
  writers. The report shows per-table bytes, the rotated footprint and the
  retained disabled-writer footprint. The latter two can overlap.
- The default local data disk can be up to 75 percent used. The check compares
  exact bytes before rounding the displayed percentage.

The command exits 1 on missing or unreadable metadata or a failed limit.
The full acceptance command requires this same readback on all data pods
before it checks the independent 2 GiB floor and creates probes.

Record backup and restore evidence separately for the SQLite metadata and
ClickHouse signal data. [Dev backup procedure](https://github.com/OpenResilienceInitiative/ORISO-Helm/blob/3302d946b1297043c27a9fa2ef521fb544d1d99f/runbooks/signoz-dev.md#replace-an-unavailable-sqlite-backup-destination) · [ClickHouse signal recovery](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/499)

## Dev operator steps for the full release gate (#494)

The gate is a script run from any checkout of origin/dev. It needs no Helm
rollout of its own, but its Java-signal and `service.version` checks measure
the installed release, so run it after the approved Dev rollout (or label the
result as covering the currently installed revision). It does not restart or
change ClickHouse.

Preconditions, all read-only:

```bash
kubectl -n caritas get chi caritas-clickhouse -o jsonpath='{.status.status}{"\n"}'   # Completed
kubectl -n caritas get sts -l clickhouse.altinity.com/chi=caritas-clickhouse        # 1/1, owned by the CHI
kubectl -n caritas get ds caritas-k8s-infra-otel-agent                              # desired = ready
```

1. Readiness only (no ingestion). `--ssh-host` runs plain `kubectl` over SSH, so
   use an account whose `kubectl` already works (`davea@178.105.70.64` does;
   `root@` is not needed). Expect a JSON report with the owned StatefulSet,
   both infrastructure collectors and the pipeline checks passed, and
   synthetic persistence, capacity and retention skipped.

   ```bash
   python3 scripts/signoz_runtime_acceptance.py --namespace caritas --release caritas \
     --environment dev --cluster-name oriso-dev --ssh-host davea@178.105.70.64 --skip-synthetic
   ```

2. Full gate (separate authorization, writes synthetic data). It creates three
   short-lived `curlimages/curl` pods (`--rm`), one BusyBox log pod and one
   `Event`, all named `signoz-acceptance-<8 hex>`. It writes one trace, one
   metric and one correlated log with service `oriso-signoz-acceptance` and the
   random `oriso.acceptance.id`. It exits before writing if the retention
   readback fails or a data pod has under 2 GiB free.

   ```bash
   python3 scripts/signoz_runtime_acceptance.py --namespace caritas --release caritas \
     --environment dev --cluster-name oriso-dev --ssh-host davea@178.105.70.64 \
     | tee /tmp/signoz-acceptance-dev.json
   ```

   Pass criteria: exit 0; `syntheticReadback` with positive trace, metric and
   log counts; positive `k8sInfraReadback`; `forbiddenLogBody` exactly `0`;
   `clickhouseRetention`, `clickhouseCapacity` and `javaSignals` populated for
   `user-service`, `agency-service`, `tenant-service`, `consulting-type-service`.
   Attach the JSON to #494.

3. Independent readback (read-only, run in
   `kubectl -n caritas exec chi-caritas-clickhouse-cluster-0-0-0 -c clickhouse -- clickhouse-client --readonly=1`;
   each count must be at least 1):

   ```sql
   SELECT 'traces', count() FROM signoz_traces.signoz_index_v3
    WHERE timestamp > now() - INTERVAL 1 HOUR AND resources_string['service.name'] = 'oriso-signoz-acceptance'
   UNION ALL
   SELECT 'logs', count() FROM signoz_logs.logs_v2
    WHERE timestamp > toUnixTimestamp(now() - INTERVAL 1 HOUR) * 1000000000
      AND resources_string['service.name'] = 'oriso-signoz-acceptance'
   UNION ALL
   SELECT 'metrics', count() FROM signoz_metrics.samples_v4
    WHERE unix_milli > toUnixTimestamp64Milli(now64() - INTERVAL 1 HOUR)
      AND fingerprint IN (SELECT fingerprint FROM signoz_metrics.time_series_v4
        WHERE resource_attrs['service.name'] = 'oriso-signoz-acceptance')
   FORMAT TSV
   ```

4. Cleanup. The probe pods and the log pod remove themselves. Confirm and
   remove leftovers after a failed run:

   ```bash
   kubectl -n caritas get pod,event -o name | grep signoz-acceptance
   ```

   Delete a leftover pod by its exact name only. The three stored rows are not deleted:
   `ALTER ... DELETE` would be an unapproved ClickHouse mutation and the rows
   are tiny and carry no application data. They expire by table TTL (traces
   seven days on Dev, checked 2026-11-21; logs and metrics by their retention
   column or interval). The Event expires under the cluster Event retention.

Rollback: nothing to roll back; the gate changes no configuration. If it fails,
read `kubectl -n caritas logs` of the named collector pod, fix the cause and
rerun only step 2. A failure at the retention readback means no probe was sent.
