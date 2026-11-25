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
