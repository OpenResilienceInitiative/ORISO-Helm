# SigNoz ClickHouse signal recovery

The ClickHouse archive covers the six SigNoz signal, analytics and metadata
databases. Recover SigNoz application settings and accounts using the separate
SQLite procedure. The current Dev ClickHouse catalog has one local
`default` disk as of 2026-11-08. Production use requires an approved backup
destination, archive retention, recovery objectives and a restore environment.
[SQLite recovery](https://github.com/OpenResilienceInitiative/ORISO-Helm/pull/496)

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
