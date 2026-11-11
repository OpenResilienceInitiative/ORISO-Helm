#!/usr/bin/env python3
"""Run the Java signal readback against native ClickHouse and synthetic rows."""

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "scripts" / "signoz_runtime_acceptance.py"
SPEC = importlib.util.spec_from_file_location("signoz_runtime_acceptance", SCRIPT)
assert SPEC and SPEC.loader
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)

END_MS = 1_750_000_000_000
SPAN_MS = END_MS - 1
TRACE_BUCKET_SECONDS = SPAN_MS // 1_800_000 * 1_800
ENVIRONMENT = "pre-dev"
SERVICES = ("user-service", "agency-service", "tenant-service", "consulting-type-service")
EXTRA_SPAN_SERVICE = "user-service"


def fixture_tables(extra_version: str) -> dict[str, str]:
    logs = []
    traces = []
    series = []
    samples = []

    def trace(service: str, identity: int, version: str) -> str:
        attrs = (
            f"map('deployment.environment', '{ENVIRONMENT}', "
            f"'service.version', {GATE._sql_literal(version)})"
        )
        return (
            f"('{service}', {attrs}, fromUnixTimestamp64Milli({SPAN_MS}), "
            f"{TRACE_BUCKET_SECONDS}, '{identity:032x}', '{identity:016x}')"
        )

    for identity, service in enumerate(SERVICES, start=1):
        attrs = f"map('service.name', '{service}', 'deployment.environment', '{ENVIRONMENT}')"
        logs.append(f"({attrs}, {SPAN_MS * 1_000_000}, '{identity:032x}', '{identity:016x}')")
        traces.append(trace(service, identity, "sha256:" + "1" * 64))
        series.append(f"('default', 'Unspecified', 'jvm.memory', {identity}, {SPAN_MS}, {attrs})")
        samples.append(f"('default', 'Unspecified', 'jvm.memory', {identity}, {SPAN_MS}, 0.0, 0)")
    traces.append(trace(EXTRA_SPAN_SERVICE, len(SERVICES) + 1, extra_version))

    # Replace only the four relations. Query predicates and result handling stay live.
    return {
        "signoz_logs.distributed_logs_v2": (
            "values('resources_string Map(String,String), timestamp UInt64, trace_id String, span_id String', "
            f"{', '.join(logs)})"
        ),
        "signoz_traces.distributed_signoz_index_v3": (
            "values('serviceName String, resources_string Map(String,String), timestamp DateTime64(9), "
            f"ts_bucket_start UInt64, trace_id FixedString(32), span_id String', {', '.join(traces)})"
        ),
        "signoz_metrics.distributed_time_series_v4": (
            "values('env String, temporality String, metric_name String, fingerprint UInt64, "
            f"unix_milli Int64, resource_attrs Map(String,String)', {', '.join(series)})"
        ),
        "signoz_metrics.distributed_samples_v4": (
            "values('env String, temporality String, metric_name String, fingerprint UInt64, "
            f"unix_milli Int64, value Float64, flags UInt32', {', '.join(samples)})"
        ),
    }


class JavaSignalClickHouseContract(unittest.TestCase):
    def read_fixture(self, extra_version: str) -> dict:
        tables = fixture_tables(extra_version)
        binary = os.environ.get("CLICKHOUSE_BINARY", "clickhouse")

        with tempfile.TemporaryDirectory(prefix="signoz-java-clickhouse-") as data_dir:
            def run_query(command: list[str]) -> subprocess.CompletedProcess[str]:
                *query_options, sql = command[-4:]
                for relation, fixture in tables.items():
                    sql = sql.replace(relation, fixture)
                return subprocess.run(
                    [binary, "local", "--path", data_dir, "--max_threads", "2", *query_options, sql],
                    stdout=subprocess.PIPE,
                    text=True,
                    check=True,
                    timeout=30,
                )

            runner = mock.Mock(spec=GATE.Runner)
            runner.run.side_effect = run_query
            with mock.patch.object(GATE.time, "time_ns", return_value=END_MS * 1_000_000):
                report = GATE._read_java_signals(runner, "fixture", "fixture", ENVIRONMENT)

        self.assertCountEqual(report["services"], SERVICES)
        self.assertEqual(report["missing"], [])
        self.assertEqual(report["services"][EXTRA_SPAN_SERVICE]["traces"], 2)
        return report

    def test_distinct_populated_versions_pass(self) -> None:
        report = self.read_fixture("sha256:" + "2" * 64)
        GATE._require_java_signals(report)

    def test_tab_only_version_fails_even_with_valid_sibling(self) -> None:
        report = self.read_fixture("\t")
        with self.assertRaisesRegex(
            RuntimeError,
            r"^Java trace identity mismatch for user-service \(0 wrong environment, 1 invalid version\)$",
        ):
            GATE._require_java_signals(report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
