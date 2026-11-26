# SigNoz ClickHouse signal recovery

The ClickHouse archive covers the six SigNoz signal, analytics and metadata
databases. Recover SigNoz application settings and accounts using the separate
SQLite procedure. The current Dev ClickHouse catalog has one local
`default` disk as of 2026-11-08. Production use requires an approved backup
destination, archive retention, recovery objectives and a restore environment.
[SQLite recovery](https://github.com/OpenResilienceInitiative/ORISO-Helm/pull/496)

## Activate the scheduled archives

A routine Dev upgrade leaves the ClickHouse signal backup inactive. For a
separately approved rollout, verify that the storage class can provision and
attach a 10 GiB ReadWriteOnce volume at the ClickHouse Pod's topology. Plan for
ClickHouse to restart when its pod template changes. Check the bound claim,
the recovered Ready replica and SigNoz queries before accepting the rollout.

In the [Dev rollout procedure](signoz-dev.md#rollout), use the same input list
for generating and checking the intended observability projection:

```sh
observability_inputs=(values-dev.yaml values-dev-clickhouse-signal-backup.yaml)
python3 scripts/observability_overlay.py "${observability_inputs[@]}" > "$observability_file"
```

Helm replaces volume and mount lists. Preserve unrelated existing entries in
the reviewed values if the installation has any. For an already enabled
installation, include the same approved overlay to keep its schedule and
mount. For an intentional removal, use a reviewed values input that sets the
job flag to false, removes only the signal backup volume and mount entries, and
replaces its disk file with empty valid XML (`<clickhouse/>`). This makes each
planned change visible to the drift check. Use explicit values rather than
reusing the installed release values. The preflight flags inherited signal
backup disk or mount settings on a routine upgrade. Plan the restart and verify
the kept claim's PV reclaim policy before removing the mount.

With the overlay active, `signoz.orisoDataProtection.signalBackup` runs a CronJob daily at
03:15 UTC. It writes a native archive of the six databases to the
`oriso_signal_backup` disk. That disk is the separate claim
`signoz-clickhouse-signal-backup`, which ClickHouse mounts at
`/var/lib/clickhouse-signal-backup/`. Helm keeps the claim on uninstall and
the retain hook switches its volume to reclaim `Retain` on the next upgrade
after it binds. Each archive `signal-<UTC stamp>.zip` passes a CRC read-back
before its `.sha256` file is written. The newest seven verified archives are
kept. Unverified leftovers of failed runs are removed. The Job's termination
message records the backup ID, archive, digest and size.

```sh
kubectl -n caritas get jobs -l app.kubernetes.io/component=signoz-signal-backup
kubectl -n caritas get pod -l app.kubernetes.io/component=signoz-signal-backup \
  -o jsonpath='{.items[-1:].status.containerStatuses[0].state.terminated.message}'
```

The claim shares ClickHouse's node and storage class. It protects against a
deleted or recreated data claim, not against the loss of the node's storage.
Restore into a fresh target as described below. Use
`Disk('oriso_signal_backup', '<archive>')` after mounting a copy of the claim
read-only as that disk and checking the `.sha256` file.

## Isolated rehearsal

Run the rehearsal on ARM64 macOS with Python 3.11 or later, PyYAML and the two
official release assets. Keep the ClickHouse download unexecuted. It
self-extracts on first execution, so the runner starts a private copy after
checking the release digest. It also verifies the Collector archive.

```sh
recovery_inputs=$(mktemp -d)
gh release download v25.12.5.44-stable -R ClickHouse/ClickHouse \
  -D "$recovery_inputs" -p clickhouse-macos-aarch64
gh release download v0.144.6 -R SigNoz/signoz-otel-collector \
  -D "$recovery_inputs" -p signoz-otel-collector_darwin_arm64.tar.gz
PYTHONDONTWRITEBYTECODE=1 python3 scripts/signoz_clickhouse_recovery_rehearsal.py \
  "$recovery_inputs/clickhouse-macos-aarch64" \
  "$recovery_inputs/signoz-otel-collector_darwin_arm64.tar.gz"
```

To compare with Dev, run the following read-only query over the existing
authorized connection and pass the result as `--dev-catalog`. It returns
table names and engines.

```sql
SELECT database, name, engine
FROM system.tables
WHERE database IN (
    'signoz_analytics', 'signoz_logs', 'signoz_metadata',
    'signoz_meter', 'signoz_metrics', 'signoz_traces'
)
ORDER BY database, name
FORMAT JSONEachRow
```

The native migrator builds the source schema. A loopback Collector writes
synthetic OTLP and stops before backup. The target starts with new storage
and Keeper and receives a digest-verified copy of the archive. The checks
compare table definitions and migration status, read correlated archived
signals, validate metadata and aggregates, and write fresh OTLP through a
restored materialized view. The rehearsal also exercises the duplicate
backup name, digest mismatch, occupied target and native malformed archive
failure paths.

The source and restored target initially contain both analytics canaries
under the archive's test retention. Applying a shorter retention interval
on the target removes the older row and preserves the fresh one. These
intervals belong to the synthetic fixture. The recorded Dev trace TTL
differs from the upstream migration default.

## Approved archive and recovery

1. Confirm the installed ClickHouse and Collector versions, the database
   scope, the shard and replica layout, and the approved backup disk. The
   rehearsal uses one shard and one replica. Validate the archive strategy
   against the approved layout before use.
2. Establish the approved capture point for the signal writers. Require
   native `BACKUP_CREATED` and record the backup identity and digest in the
   protected recovery records. Keep the archive under the access and
   retention policy for stored telemetry, on a destination independent of
   the source disk.
3. Provision a separate, access-restricted target with fresh ClickHouse and
   Keeper state. Check that all six databases are absent and keep the
   application and migration jobs stopped until the restore is complete.
4. Verify the archive digest on the target. Require native `RESTORED` and
   compare the catalog, migrations, replica state and approved signal
   readbacks before accepting the recovered instance. A failed native
   restore may leave tables or parts behind. Use a fresh target for a retry.
5. Apply and verify the current approved signal retention before reopening
   access. Validate expired and retained records separately from archive
   retention. Record the recovery point and elapsed recovery time from the
   approved representative archive.

The following is the single-shard SQL used by the rehearsal. Replace the
disk and archive names with the approved values in their respective source
and isolated target sessions.

```sql
BACKUP DATABASE signoz_analytics, DATABASE signoz_logs,
       DATABASE signoz_metadata, DATABASE signoz_meter,
       DATABASE signoz_metrics, DATABASE signoz_traces
TO Disk('reviewed_backup_disk', 'signoz-signal-archive.zip')
```

Run the restore statement in the isolated target session.

```sql
RESTORE DATABASE signoz_analytics, DATABASE signoz_logs,
        DATABASE signoz_metadata, DATABASE signoz_meter,
        DATABASE signoz_metrics, DATABASE signoz_traces
FROM Disk('reviewed_backup_disk', 'signoz-signal-archive.zip')
SETTINGS allow_non_empty_tables = 0
```

[ClickHouse backup and restore to disk](https://clickhouse.com/docs/concepts/features/backup-restore/local-disk)
· [Native backup and restore status](https://clickhouse.com/docs/reference/system-tables/backups)
· [Production telemetry boundary](https://github.com/OpenResilienceInitiative/ORISO-Docs/pull/261)

## Dev operator steps for the protected archive (#499)

Read-only on 2026-11-21: Dev runs ClickHouse 25.12.5.44 with one `default`
disk, zero rows in `system.backups`, 133 tables in the six databases, 316 MiB
of active parts, a seven-day trace TTL, and a 25 GiB data claim on
`hcloud-volumes` (WaitForFirstConsumer, reclaim Delete). No signal-backup
claim or CronJob exists. Nothing below has been run on Dev.

### Decision first: enable the Dev signal backup or not

ClickHouse refuses `BACKUP` to any disk that is not on its allowlist, so a
native archive on Dev needs the `oriso_signal_backup` disk and its volume.

- **Path A, enable it.** Adds a 10 GiB claim, a mount and the disk file to the
  ClickHouseInstallation. The operator restarts the ClickHouse pod, so SigNoz
  queries and ingestion pause until it is Ready again. Daily archives at
  03:15 UTC, seven kept. Use the opt-in overlay
  `values-dev-clickhouse-signal-backup.yaml` once PR #611 has merged. Until
  then origin/dev's `values-dev.yaml` already sets these keys, so any Dev
  observability rollout of that file (#513, #494) takes Path A implicitly.
  Merge #611 first, or remove those keys from the projection, so the decision
  is not made by accident.
- **Path B, do not enable it.** The chart has no other destination. Without the
  disk no native archive and no restore evidence can be produced, so the
  third acceptance item of #499 stays open. A different destination (an
  object-store disk) needs chart work, credentials and approval, and is not
  prepared here.

### Path A steps

**Dev volume pre-provisioned on 21 November 2026.** The claim
`signoz-clickhouse-signal-backup` exists and is Bound: 10 GiB on
`hcloud-volumes`, volume `pvc-b9af13bd-8ff8-42d1-9199-62bc5e89dc36`, reclaim
policy already `Retain`. A short test pod on the ClickHouse node
`oriso-dev-workers-79a6f89ec12a5d07` attached, formatted and wrote to it, then
was deleted, so the volume is detached. The claim carries Helm's ownership
metadata (release `caritas`, `app.kubernetes.io/managed-by: Helm`), so the
upgrade adopts it instead of failing on an existing object. On Dev the rollout
therefore only re-attaches a known-good volume to the same node. In step 1 the
claim shows as adopted rather than new. Step 3's second upgrade is not needed;
run only its verification.

Read-only preconditions:

```bash
kubectl get sc hcloud-volumes -o jsonpath='{.allowVolumeExpansion} {.volumeBindingMode}{"\n"}'
kubectl -n caritas get pod chi-caritas-clickhouse-cluster-0-0-0 -o wide   # node and zone for the new volume
kubectl -n caritas get chi caritas-clickhouse -o jsonpath='{.status.status}{"\n"}'   # Completed
```

1. ROLLOUT. Follow [the Dev rollout](signoz-dev.md#rollout) with the overlay in
   the same input list for the projection and the preflight. The chart render
   fails without the mount, the disk file, or with more than one shard or
   replica. Review the server dry-run: only the ClickHouseInstallation, the
   data-protection ConfigMap, the new claim and the CronJob may change, and the
   25 GiB claim template must not. Record the ClickHouse pod UID and start
   time first, as that runbook describes. Blast radius: one ClickHouse
   restart, plus whatever else the rollout it joins restarts.
2. ROLLOUT. After the upgrade check: CHI `Completed`, pod `1/1`, a SigNoz query
   works, the claim is `Bound`, and `SELECT name, path FROM system.disks` lists
   `oriso_signal_backup` at `/var/lib/clickhouse-signal-backup/`.
3. ROLLOUT. Run the same `helm upgrade` once more. The retain hook only sees
   volumes that exist when Helm renders, and it switches the claim's volume
   to `Retain` after the claim binds. Verify:

   ```bash
   pv=$(kubectl -n caritas get pvc signoz-clickhouse-signal-backup -o jsonpath='{.spec.volumeName}')
   kubectl get pv "$pv" -o jsonpath='{.spec.persistentVolumeReclaimPolicy}{"\n"}'   # Retain
   ```

4. ROLLOUT, separate authorization (writes one archive to the new claim). Do
   not wait for 03:15 UTC:

   ```bash
   job=signal-backup-manual-$(date -u +%Y%m%d%H%M%S)
   kubectl -n caritas create job --from=cronjob/caritas-signoz-data-protection-signal-backup "$job"
   kubectl -n caritas wait --for=condition=complete "job/$job" --timeout=20m
   kubectl -n caritas get pod -l job-name="$job" \
     -o jsonpath='{.items[0].status.containerStatuses[0].state.terminated.message}{"\n"}'
   ```

   Expect JSON with `backupId`, `archive` (`signal-<UTC>.zip`), `sha256` and
   `bytes`. Record them and the elapsed time. Read it back independently
   (read-only, `clickhouse-client --readonly=1`). Expect `BACKUP_CREATED` for
   that `backupId`:

   ```sql
   SELECT id, status, num_files, formatReadableSize(total_size) AS size, start_time, end_time
   FROM system.backups ORDER BY start_time DESC LIMIT 3 FORMAT TSV
   ```

   Capture the source reference at the same time, for the restore comparison:

   ```sql
   SELECT database, name, total_rows, extract(engine_full, 'TTL .*') AS ttl
   FROM system.tables
   WHERE database IN ('signoz_analytics','signoz_logs','signoz_metadata','signoz_meter','signoz_metrics','signoz_traces')
     AND engine LIKE '%MergeTree' ORDER BY database, name FORMAT TSV
   ```

5. Copy the archive to the approved protected destination. The claim shares
   the source's cloud account and node storage class, so it is not independent.
   Stream it over a read-only exec and check the digest against the Job
   receipt:

   ```bash
   a=signal-<UTC stamp>.zip
   for f in "$a" "$a.sha256"; do
     kubectl -n caritas exec chi-caritas-clickhouse-cluster-0-0-0 -c clickhouse -- \
       cat "/var/lib/clickhouse-signal-backup/$f" > "$f"
   done
   sha256sum -c "$a.sha256"      # prints OK; the digest must equal the receipt sha256
   ```

   Store both files under the approved access and retention policy. Record
   the destination, retention period and readers.

### Restore into a separate environment (both paths need an archive)

Use a machine that cannot reach Dev services and holds no Dev credentials.
This recipe uses the installed image with embedded Keeper. The same disk and
statements round-tripped a replicated table in a local test with this config.
A full six-database restore is not yet rehearsed with it. Create
`restore/config.d/90-restore-target.xml`:

```xml
<clickhouse>
  <macros><cluster>cluster</cluster><shard>01</shard><replica>restore1</replica></macros>
  <default_replica_path>/clickhouse/tables/{uuid}/{shard}</default_replica_path>
  <default_replica_name>{replica}</default_replica_name>
  <remote_servers><cluster><shard><internal_replication>true</internal_replication>
    <replica><host>127.0.0.1</host><port>9000</port></replica>
  </shard></cluster></remote_servers>
  <keeper_server>
    <tcp_port>9181</tcp_port><server_id>1</server_id>
    <log_storage_path>/var/lib/clickhouse/coordination/log</log_storage_path>
    <snapshot_storage_path>/var/lib/clickhouse/coordination/snapshots</snapshot_storage_path>
    <raft_configuration><server><id>1</id><hostname>127.0.0.1</hostname><port>9234</port></server></raft_configuration>
  </keeper_server>
  <zookeeper><node><host>127.0.0.1</host><port>9181</port></node></zookeeper>
  <distributed_ddl><path>/clickhouse/ddl</path></distributed_ddl>
  <storage_configuration><disks><oriso_signal_backup>
    <type>local</type><path>/var/lib/clickhouse-signal-backup/</path>
  </oriso_signal_backup></disks></storage_configuration>
  <backups><allowed_disk>oriso_signal_backup</allowed_disk></backups>
</clickhouse>
```

```bash
cd restore && (cd backups && sha256sum -c "$a.sha256")     # digest before anything starts
docker run -d --name restore-ch \
  -v "$PWD/config.d:/etc/clickhouse-server/config.d:ro" \
  -v "$PWD/backups:/var/lib/clickhouse-signal-backup:ro" \
  docker.io/clickhouse/clickhouse-server:25.12.5
docker exec restore-ch clickhouse-client -q "SELECT count() FROM system.databases WHERE name LIKE 'signoz%'"   # 0
start=$(date +%s)
docker exec restore-ch clickhouse-client --format TSV -q "
  RESTORE DATABASE signoz_analytics, DATABASE signoz_logs, DATABASE signoz_metadata,
          DATABASE signoz_meter, DATABASE signoz_metrics, DATABASE signoz_traces
  FROM Disk('oriso_signal_backup', '$a') SETTINGS allow_non_empty_tables = 0"
echo "recovery seconds: $(( $(date +%s) - start ))"
```

Expect one row with `RESTORED`. After any failure remove the container and its
data and start a new one. Do not retry on the same state.

Then verify and record:

- 133 tables exist across the six databases
  (`SELECT count() FROM system.tables WHERE database LIKE 'signoz%'`).
- The row-count and TTL query from step 4 matches the source reference. Live
  Dev keeps growing, so compare with the reference, not with current Dev.
- `system.replicas` shows no readonly replica or lost session.
- The TTL clauses equal the approved retention (Dev traces seven days) before
  anyone treats the data as a recovered environment.
- Independent application readback: run SigNoz (installed image, with the
  SQLite archive restored per #524) against this server. Compare the Java
  signal counts and one trace by ID through its query API. This step is not
  rehearsed here.

Cleanup: `docker rm -f restore-ch`, delete `restore/`, and remove scratch copies
of the archive under the approved procedure.

### Rollback (Path A)

Set the job flag to `false`, remove only the signal volume and mount entries
and replace the disk file with `<clickhouse/>`, as described above. This
restarts ClickHouse once more. The claim has `helm.sh/resource-policy: keep`
and a `Retain` volume, so the archives stay. Delete the claim only with
explicit approval after the last verified archive is stored elsewhere.
