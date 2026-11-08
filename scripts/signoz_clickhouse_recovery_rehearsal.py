#!/usr/bin/env python3
"""Rehearse SigNoz ClickHouse recovery with pinned native ARM64 macOS releases."""

import argparse
import hashlib
import http.client
import json
import platform
import re
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from xml.sax.saxutils import escape

import yaml

from signoz_runtime_acceptance import CLICKHOUSE_EXPORTERS, build_otlp_payloads, build_readback_queries

DATABASES = ("signoz_analytics", "signoz_logs", "signoz_metadata", "signoz_meter", "signoz_metrics", "signoz_traces")
DB_LIST = ", ".join(repr(name) for name in DATABASES)
BACKUP_SCOPE = ", ".join(f"DATABASE {name}" for name in DATABASES)
ARCHIVE = "signals.zip"
ENVIRONMENT = "recovery-rehearsal"
BACKUP_SQL = f"BACKUP {BACKUP_SCOPE} TO Disk('rehearsal_backups', '{ARCHIVE}')"
CLICKHOUSE_VERSION = "25.12.5.44"
CLICKHOUSE_SHA256 = "1a0edc37c6e5aa6c06a7cb00c8f8edd83a0df02f643e29185a8b3934eb860ac4"
COLLECTOR_SHA256 = "493ad6b899e2aa28304b9fac44c80104783adfaf90f051c0d2b32fbed82ffc42"
COLLECTOR_MEMBER = "signoz-otel-collector_darwin_arm64/bin/signoz-otel-collector"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def server_config(directory, backup, tcp, keeper, interserver):
    root = escape(str(directory))
    backup = escape(str(backup))
    return f"""<clickhouse>
  <logger><level>information</level><console>1</console></logger>
  <listen_host>127.0.0.1</listen_host>
  <tcp_port>{tcp}</tcp_port>
  <interserver_http_host>127.0.0.1</interserver_http_host>
  <interserver_http_port>{interserver}</interserver_http_port>
  <path>{root}/data/</path>
  <tmp_path>{root}/tmp/</tmp_path>
  <user_files_path>{root}/user-files/</user_files_path>
  <format_schema_path>{root}/format-schemas/</format_schema_path>
  <users_config>{root}/users.xml</users_config>
  <mark_cache_size>67108864</mark_cache_size>
  <uncompressed_cache_size>0</uncompressed_cache_size>
  <max_server_memory_usage>1073741824</max_server_memory_usage>
  <max_thread_pool_size>192</max_thread_pool_size>
  <background_pool_size>8</background_pool_size>
  <background_schedule_pool_size>8</background_schedule_pool_size>
  <background_common_pool_size>4</background_common_pool_size>
  <background_distributed_schedule_pool_size>4</background_distributed_schedule_pool_size>
  <background_message_broker_schedule_pool_size>2</background_message_broker_schedule_pool_size>
  <background_fetches_pool_size>4</background_fetches_pool_size>
  <background_move_pool_size>2</background_move_pool_size>
  <background_buffer_flush_schedule_pool_size>2</background_buffer_flush_schedule_pool_size>
  <default_replica_path>/clickhouse/tables/{{uuid}}/{{shard}}</default_replica_path>
  <default_replica_name>{{replica}}</default_replica_name>
  <macros><cluster>cluster</cluster><shard>01</shard><replica>replica1</replica></macros>
  <remote_servers><cluster><shard><internal_replication>true</internal_replication>
    <replica><host>127.0.0.1</host><port>{tcp}</port></replica>
  </shard></cluster></remote_servers>
  <merge_tree>
    <number_of_free_entries_in_pool_to_execute_mutation>4</number_of_free_entries_in_pool_to_execute_mutation>
    <number_of_free_entries_in_pool_to_execute_optimize_entire_partition>4</number_of_free_entries_in_pool_to_execute_optimize_entire_partition>
    <number_of_free_entries_in_pool_to_lower_max_size_of_merge>4</number_of_free_entries_in_pool_to_lower_max_size_of_merge>
  </merge_tree>
  <zookeeper><node><host>127.0.0.1</host><port>{keeper}</port></node></zookeeper>
  <distributed_ddl><path>/clickhouse/ddl</path></distributed_ddl>
  <storage_configuration><disks><rehearsal_backups>
    <type>local</type><path>{backup}/</path>
  </rehearsal_backups></disks></storage_configuration>
  <backups><allowed_disk>rehearsal_backups</allowed_disk></backups>
</clickhouse>
"""


def keeper_config(directory, port, raft):
    root = escape(str(directory))
    return f"""<clickhouse>
  <logger><level>information</level><console>1</console></logger>
  <listen_host>127.0.0.1</listen_host>
  <keeper_server>
    <tcp_port>{port}</tcp_port><server_id>1</server_id>
    <log_storage_path>{root}/keeper/log/</log_storage_path>
    <snapshot_storage_path>{root}/keeper/snapshot/</snapshot_storage_path>
    <raft_configuration><server><id>1</id><hostname>127.0.0.1</hostname><port>{raft}</port></server></raft_configuration>
  </keeper_server>
</clickhouse>
"""


USERS = """<clickhouse>
  <profiles><default><max_memory_usage>536870912</max_memory_usage><max_threads>4</max_threads>
    <log_queries>0</log_queries><async_insert>0</async_insert>
  </default></profiles>
  <users><default><password></password><networks><ip>127.0.0.1</ip></networks>
    <profile>default</profile><quota>default</quota><access_management>1</access_management>
  </default></users>
  <quotas><default><interval><duration>3600</duration><queries>0</queries></interval></default></quotas>
</clickhouse>
"""


def allocate_ports(count):
    reservations = [socket.socket() for _ in range(count)]
    try:
        for reservation in reservations:
            reservation.bind(("127.0.0.1", 0))
        return [reservation.getsockname()[1] for reservation in reservations]
    finally:
        for reservation in reservations:
            reservation.close()


@contextmanager
def run_process(command, log, env):
    with log.open("wb") as sink:
        process = subprocess.Popen(command, env=env, stdout=sink, stderr=subprocess.STDOUT)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=16)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise RuntimeError(f"Process did not shut down cleanly\n{log.read_text()[-2800:]}")


@contextmanager
def server(binary, directory, backup):
    directory.mkdir(mode=0o700)
    backup.mkdir(mode=0o700, exist_ok=True)
    port, keeper, raft, interserver = allocate_ports(4)
    config = directory / "config.xml"
    config.write_text(server_config(directory, backup, port, keeper, interserver))
    keeper_file = directory / "keeper.xml"
    keeper_file.write_text(keeper_config(directory, keeper, raft))
    (directory / "users.xml").write_text(USERS)
    client_file = directory / "client.xml"
    client_file.write_text("<clickhouse/>")
    log = directory / "server.log"
    keeper_log = directory / "keeper.log"

    def query(sql, timeout=45, check=True):
        result = subprocess.run(
            [str(binary), "client", "--config-file", str(client_file), "--host", "127.0.0.1",
             "--port", str(port), "--user", "default", "--multiquery"],
            input=sql, text=True, capture_output=True, env={}, timeout=timeout,
        )
        if check and result.returncode:
            raise RuntimeError(result.stderr[-3000:])
        return result

    env = {"CLICKHOUSE_WATCHDOG_ENABLE": "0"}
    with run_process([str(binary), "keeper", "--config-file", str(keeper_file)], keeper_log, env) as keeper_process:
        deadline = time.monotonic() + 18
        while keeper_process.poll() is None and time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", keeper), timeout=0.5) as sock:
                    sock.sendall(b"ruok")
                    if sock.recv(64) == b"imok":
                        break
            except OSError:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError(keeper_log.read_text()[-3600:])
        with run_process([str(binary), "server", "--config-file", str(config)], log, env) as process:
            deadline = time.monotonic() + 22
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    if "Ready for connections." in log.read_text() and (
                        query("SELECT version()", timeout=2, check=False).stdout.strip() == CLICKHOUSE_VERSION
                    ):
                        break
                except subprocess.TimeoutExpired:
                    pass
                time.sleep(0.2)
            else:
                raise RuntimeError(log.read_text()[-3800:])
            yield query, port


def migrate(collector, port, *steps):
    for step in steps:
        print("MIGRATE", step, flush=True)
        result = subprocess.run([str(collector), "--clickhouse-dsn", f"tcp://127.0.0.1:{port}",
                                 "--clickhouse-cluster", "cluster", "--clickhouse-replication=true",
                                 "migrate", *step.split()],
                                env={}, text=True, capture_output=True, timeout=120)
        if result.returncode:
            raise RuntimeError((result.stderr + result.stdout)[-4000:])


def catalog(query):
    rows = query(f"""SELECT database, name, engine, create_table_query FROM system.tables
        WHERE database IN ({DB_LIST}) ORDER BY database, name FORMAT JSONEachRow""").stdout
    return [
        {**row, "create_table_query": re.sub(r"\bUUID '[0-9a-f-]{36}'", "UUID '<generated>'", row["create_table_query"])}
        for row in map(json.loads, rows.splitlines())
    ]


def compare_inventory(schema, dev_catalog):
    def names(rows):
        return {(row["database"], row["name"], row["engine"]) for row in rows}

    expected = names(map(json.loads, dev_catalog.read_text(encoding="utf-8").splitlines()))
    actual = names(schema)
    require(actual == expected,
            f"Catalog differs from Dev. Missing {sorted(expected - actual)}, extra {sorted(actual - expected)}")
    print(f"Dev catalog matches all {len(actual)} names and engines", flush=True)


def publish_otlp(collector, clickhouse_port, directory, query, label):
    dsn = f"tcp://127.0.0.1:{clickhouse_port}"
    otlp_port, = allocate_ports(1)
    config = {
        "receivers": {"otlp": {"protocols": {"http": {"endpoint": f"127.0.0.1:{otlp_port}"}}}},
        "processors": {"batch": {"timeout": "150ms"}},
        "connectors": {"signozmeter": {
            "dimensions": [{"name": "service.name"}, {"name": "deployment.environment"}],
            "metrics_flush_interval": "500ms",
        }},
        "exporters": {
            "clickhousetraces": {"datasource": dsn, "use_new_schema": True},
            "signozclickhousemetrics": {"dsn": dsn},
            "clickhouselogsexporter": {"dsn": dsn, "use_new_schema": True},
            "metadataexporter": {"dsn": dsn, "enabled": True, "tenant_id": "1", "cache": {"provider": "in_memory"}},
            "signozclickhousemeter": {"dsn": dsn},
        },
        "service": {"telemetry": {"metrics": {"level": "none"}}, "pipelines": {
            **{signal: {"receivers": ["otlp"], "processors": ["batch"],
                        "exporters": [sink, "metadataexporter", "signozmeter"]}
               for signal, sink in CLICKHOUSE_EXPORTERS.items()},
            "metrics/meter": {"receivers": ["signozmeter"], "exporters": ["signozclickhousemeter"]},
        }},
    }
    path = directory / "collector.yaml"
    path.write_text(yaml.safe_dump(config))
    log = directory / "collector.log"
    args = [str(collector), "--config", str(path), "--copy-path", str(directory / "collector-copy.yaml")]
    with run_process(args, log, {}) as process:
        deadline = time.monotonic() + 20
        while process.poll() is None and time.monotonic() < deadline:
            if "Everything is ready. Begin running and processing data." in log.read_text():
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(log.read_text()[-3500:])
        trace_id, span_id = uuid.uuid4().hex, uuid.uuid4().hex[:16]
        payloads = build_otlp_payloads(label, ENVIRONMENT, time.time_ns(), trace_id, span_id)
        for signal, payload in payloads.items():
            connection = http.client.HTTPConnection("127.0.0.1", otlp_port, timeout=4)
            try:
                connection.request("POST", f"/v1/{signal}", json.dumps(payload), {"Content-Type": "application/json"})
                response = connection.getresponse()
                if response.status != 200:
                    raise RuntimeError(f"{signal} write returned {response.status}: {response.read()}")
                response.read()
            finally:
                connection.close()
        counts = {}
        deadline = time.monotonic() + 28
        while process.poll() is None and time.monotonic() < deadline:
            counts = readback(query, label)
            if counts == {"traces": 1, "metrics": 1, "logs": 1}:
                stored = correlated_rows(query, label)
                require(stored == {
                    "trace": f"{trace_id}\t{span_id}\tsignoz.runtime.acceptance",
                    "log": f"{trace_id}\t{span_id}\tSigNoz runtime acceptance canary",
                }, "Stored trace and log did not match the exported payloads")
                # Let the usage connector flush its local rollup before stopping the writer.
                time.sleep(1.2)
                return stored
            time.sleep(0.25)
        raise RuntimeError(f"Canary write was not visible: {counts}\n{log.read_text()[-4800:]}")


def readback(query, label):
    return {signal: int(query(sql).stdout) for signal, sql in build_readback_queries(ENVIRONMENT, label).items()}


def correlated_rows(query, label):
    condition = f"WHERE resources_string['oriso.acceptance.id'] = '{label}'"
    return {
        "trace": query("SELECT trace_id, span_id, name FROM signoz_traces.distributed_signoz_index_v3 "
                       + condition).stdout.strip(),
        "log": query("SELECT trace_id, span_id, body FROM signoz_logs.distributed_logs_v2 "
                     + condition).stdout.strip(),
    }


def checksum(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def backup_result(query, sql, state):
    result = query(sql, timeout=160).stdout.strip().split("\t")
    if len(result) != 2 or result[1] != state:
        raise RuntimeError(f"Unexpected backup result: {result}")


def restore(query, archive, digest):
    if checksum(archive) != digest:
        raise ValueError("Archive checksum differs")
    if int(query(f"SELECT count() FROM system.databases WHERE name IN ({DB_LIST})").stdout):
        raise ValueError("Restore target already has a SigNoz database")
    backup_result(query, f"RESTORE {BACKUP_SCOPE} FROM Disk('rehearsal_backups', '{ARCHIVE}') "
                         "SETTINGS allow_non_empty_tables=0", "RESTORED")


def seed_analytics(query):
    query("""INSERT INTO signoz_analytics.rule_state_history_v0 (_retention_days, rule_id, unix_milli)
        SELECT 3, 'recovery-fresh', toUnixTimestamp64Milli(now64(3))
        UNION ALL
        SELECT 3, 'recovery-old', toUnixTimestamp64Milli(now64(3) - INTERVAL 2 DAY)
    """)


def rule_ids(query):
    return query("""SELECT rule_id FROM signoz_analytics.distributed_rule_state_history_v0
        WHERE startsWith(rule_id, 'recovery-') ORDER BY rule_id""").stdout.splitlines()


def aggregate_value(query, label):
    count, value = query(f"""SELECT sum(count), sum(sum) FROM signoz_metrics.distributed_samples_v4_agg_5m
        WHERE (env, temporality, metric_name, fingerprint) IN
            (SELECT env, temporality, metric_name, fingerprint FROM signoz_metrics.time_series_v4
             WHERE resource_attrs['oriso.acceptance.id'] = '{label}')
    """).stdout.strip().split("\t")
    return int(count), float(value)


def metadata_counts(query):
    return query("""SELECT
        (SELECT count() FROM signoz_metadata.attributes_metadata),
        (SELECT count() FROM signoz_meter.samples_agg_1d)
    """).stdout.strip()


def ledger(query):
    return {db: query(f"SELECT migration_id, status FROM {db}.schema_migrations_v2 "
                      "ORDER BY migration_id, status").stdout for db in DATABASES}


def verify_replicas(query, count):
    expected = f"{count}\t0"
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        actual = query(f"""SELECT count(), countIf(is_readonly OR is_session_expired OR active_replicas != 1)
            FROM system.replicas WHERE database IN ({DB_LIST})""").stdout.strip()
        if actual == expected:
            return
        time.sleep(0.25)
    raise RuntimeError(f"Replica readiness differs: {actual}")


def require_rejection(action, reason):
    try:
        action()
    except ValueError as error:
        if str(error) == reason:
            return
        raise
    raise RuntimeError(f"Expected rejection: {reason}")


def rehearse(binary, collector, directory, dev_catalog):
    label = "before-backup"
    with server(binary, directory / "source", directory / "source-backup") as (query, port):
        print(query("SELECT version()").stdout.strip(), flush=True)
        migrate(collector, port, "bootstrap", "sync up", "async up")
        expected_schema = catalog(query)
        require({row["database"] for row in expected_schema} == set(DATABASES), "Migrated database scope differs")
        if dev_catalog is not None:
            compare_inventory(expected_schema, dev_catalog)
        print(f"Migrated {len(expected_schema)} tables", flush=True)
        seed_analytics(query)
        expected_pair = publish_otlp(collector, port, directory / "source", query, label)
        print("Persisted correlated trace and log and one metric sample", flush=True)
        expected_rules = rule_ids(query)
        require(expected_rules == ["recovery-fresh", "recovery-old"], "Source retention markers are missing")
        require(aggregate_value(query, label) == (1, 1.0), "Source gauge rollup differs")
        expected_metadata = metadata_counts(query)
        require(all(int(count) > 0 for count in expected_metadata.split("\t")), "Source metadata or meter rollup is empty")
        expected_ledger = ledger(query)
        backup_result(query, BACKUP_SQL, "BACKUP_CREATED")
        original = directory / "source-backup" / ARCHIVE
        digest = checksum(original)
        duplicate = query(BACKUP_SQL, check=False)
        require(duplicate.returncode != 0 and "BACKUP_ALREADY_EXISTS" in duplicate.stderr,
                "Native backup did not reject the duplicate archive name")
        require(checksum(original) == digest, "Duplicate backup changed the first archive")
        print("Duplicate backup rejected and archive checksum preserved", flush=True)
    print(f"Archived {original.stat().st_size} bytes", flush=True)
    target_backup = directory / "target-backup"
    target_backup.mkdir(mode=0o700)
    restored = target_backup / ARCHIVE
    with server(binary, directory / "target", target_backup) as (query, port):
        restored.write_bytes(b"wrong archive")
        require_rejection(lambda: restore(query, restored, digest), "Archive checksum differs")
        require(query("SELECT count() FROM system.backups").stdout.strip() == "0", "Invalid checksum reached native restore")
        shutil.copyfile(original, restored)
        restored.chmod(0o400)
        restore(query, restored, digest)
        require(catalog(query) == expected_schema, "Restored schema differs")
        require(readback(query, label) == {"traces": 1, "metrics": 1, "logs": 1}, "Restored signal counts differ")
        require(correlated_rows(query, label) == expected_pair, "Restored trace or log content differs")
        require(ledger(query) == expected_ledger, "Restored migration state differs")
        require(metadata_counts(query) == expected_metadata, "Restored metadata or meter rollup differs")
        require(aggregate_value(query, label) == (1, 1.0), "Restored gauge rollup differs")
        verify_replicas(query, sum(row["engine"].startswith("Replicated") for row in expected_schema))
        migrate(collector, port, "sync check")
        print("Restored every table, archived signals, metadata, meter rollup and migration state", flush=True)
        require(rule_ids(query) == expected_rules, "Restore did not preserve both analytics rows")
        query("""ALTER TABLE signoz_analytics.rule_state_history_v0
            UPDATE _retention_days = 1 WHERE startsWith(rule_id, 'recovery-') SETTINGS mutations_sync=2;
            ALTER TABLE signoz_analytics.rule_state_history_v0 MATERIALIZE TTL SETTINGS mutations_sync=2
        """)
        require(rule_ids(query) == ["recovery-fresh"], "TTL did not remove only the expired row")
        print("Applied shorter retention after restore and removed only the older analytics row", flush=True)
        publish_otlp(collector, port, directory / "target", query, "after-restore")
        require(aggregate_value(query, "after-restore") == (1, 1.0), "Restored view did not process the new metric")
        print("Restored metric materialized view processed the new gauge", flush=True)
    with server(binary, directory / "occupied", target_backup) as (query, _):
        query("CREATE DATABASE signoz_logs")
        require_rejection(lambda: restore(query, restored, digest), "Restore target already has a SigNoz database")
        require(query("SELECT count() FROM system.backups").stdout.strip() == "0", "Occupied target reached native restore")
        print("Empty pre-existing SigNoz database prevented restore before native SQL", flush=True)
    corrupt_backup = directory / "corrupt-backup"
    corrupt_backup.mkdir(mode=0o700)
    corrupt = corrupt_backup / ARCHIVE
    corrupt.write_bytes(b"not a native archive")
    with server(binary, directory / "corrupt", corrupt_backup) as (query, _):
        try:
            restore(query, corrupt, checksum(corrupt))
        except RuntimeError:
            require(query("SELECT status FROM system.backups").stdout.strip() == "RESTORE_FAILED",
                    "Malformed archive did not reach a failed native restore")
            print("Native restore rejected malformed archive with a matching digest", flush=True)
        else:
            raise RuntimeError("Native restore accepted a malformed archive")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clickhouse", type=Path, help="ClickHouse v25.12.5.44 macOS ARM64 release binary")
    parser.add_argument("collector", type=Path, help="SigNoz Collector v0.144.6 Darwin ARM64 release archive")
    parser.add_argument("--dev-catalog", type=Path, help="Optional read-only Dev catalog in JSONEachRow format")
    args = parser.parse_args()
    require((platform.system(), platform.machine()) == ("Darwin", "arm64"), "Use native ARM64 macOS for this rehearsal")
    with tempfile.TemporaryDirectory(prefix="signoz-ch-recovery-") as scratch:
        directory = Path(scratch)
        binary = directory / "clickhouse"
        collector = directory / "signoz-otel-collector"
        shutil.copyfile(args.clickhouse, binary)
        require(checksum(binary) == CLICKHOUSE_SHA256, "Wrong ClickHouse release SHA-256")
        binary.chmod(0o700)
        with args.collector.open("rb") as asset:
            require(hashlib.file_digest(asset, "sha256").hexdigest() == COLLECTOR_SHA256, "Wrong Collector release SHA-256")
            asset.seek(0)
            with tarfile.open(fileobj=asset, mode="r:gz") as release:
                member = release.getmember(COLLECTOR_MEMBER)
                require(member.isfile(), "Collector archive member must be a regular file")
                with release.extractfile(member) as source, collector.open("xb") as destination:
                    shutil.copyfileobj(source, destination)
        collector.chmod(0o700)
        rehearse(binary, collector, directory, args.dev_catalog)


if __name__ == "__main__":
    main()
