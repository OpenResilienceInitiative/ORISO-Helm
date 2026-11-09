#!/usr/bin/env python3
"""Unit contracts for the executable SigNoz runtime acceptance gate."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "scripts" / "signoz_runtime_acceptance.py"
SPEC = importlib.util.spec_from_file_location("signoz_runtime_acceptance", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PayloadContractTest(unittest.TestCase):
    def test_payloads_emit_correlated_privacy_safe_signals(self) -> None:
        payloads = MODULE.build_otlp_payloads(
            acceptance_id="acceptance-1234",
            environment="predev",
            timestamp_ns=1_750_000_000_000_000_000,
            trace_id="0123456789abcdef0123456789abcdef",
            span_id="0123456789abcdef",
        )

        self.assertEqual(set(payloads), {"traces", "metrics", "logs"})
        encoded = json.dumps(payloads, sort_keys=True)
        self.assertIn(
            '"service.name", "value": {"stringValue": "oriso-signoz-acceptance"}',
            encoded,
        )
        self.assertIn(
            '"deployment.environment", "value": {"stringValue": "predev"}', encoded
        )
        self.assertIn(
            '"oriso.acceptance.id", "value": {"stringValue": "acceptance-1234"}',
            encoded,
        )
        self.assertIn('"name": "oriso.signoz.acceptance"', encoded)
        self.assertIn('"traceId": "0123456789abcdef0123456789abcdef"', encoded)
        self.assertIn('"spanId": "0123456789abcdef"', encoded)

        forbidden = ("email", "password", "token", "message.body", "username")
        for name in forbidden:
            self.assertNotIn(name, encoded.lower())

    def test_application_log_probe_contains_a_marker_that_must_be_removed(self) -> None:
        line = MODULE.build_application_log_line(
            acceptance_id="acceptance-1234",
            trace_id="0123456789abcdef0123456789abcdef",
            span_id="0123456789abcdef",
            forbidden_marker="forbidden-marker-1234",
        )

        payload = json.loads(line)
        self.assertEqual(payload["serviceName"], "oriso-signoz-log-acceptance")
        self.assertEqual(payload["traceId"], "0123456789abcdef0123456789abcdef")
        self.assertEqual(payload["spanId"], "0123456789abcdef")
        self.assertEqual(payload["log"]["level"], "INFO")
        self.assertEqual(payload["log"]["logger"], "SigNozAcceptance")
        self.assertIn("forbidden-marker-1234", payload["log"]["message"])
        self.assertIn("forbidden-marker-1234", payload["log"]["stack"])
        self.assertEqual(payload["orisoAcceptanceId"], "acceptance-1234")


class ReadbackContractTest(unittest.TestCase):
    def test_queries_cover_all_three_signals_and_runtime_identity(self) -> None:
        queries = MODULE.build_readback_queries(
            environment="dev",
            acceptance_id="acceptance-1234",
            service_name="oriso-signoz-acceptance",
            metric_name="oriso.signoz.acceptance",
        )

        self.assertEqual(set(queries), {"traces", "metrics", "logs"})
        for query in queries.values():
            self.assertIn("deployment.environment", query)
            self.assertIn("oriso-signoz-acceptance", query)
            self.assertIn("oriso.acceptance.id", query)
            self.assertIn("acceptance-1234", query)
        self.assertIn("signoz_traces.signoz_index_v3", queries["traces"])
        self.assertIn("signoz_metrics.time_series_v4", queries["metrics"])
        self.assertIn("signoz_metrics.samples_v4", queries["metrics"])
        self.assertIn("unix_milli", queries["metrics"])
        self.assertIn("signoz_logs.logs_v2", queries["logs"])

    def test_infra_queries_cover_kubernetes_host_collector_events_and_privacy(
        self,
    ) -> None:
        queries = MODULE.build_infra_readback_queries(
            environment="pre-dev",
            cluster_name="oriso-predev",
            acceptance_id="acceptance-1234",
            trace_id="0123456789abcdef0123456789abcdef",
            forbidden_marker="forbidden-marker-1234",
        )

        self.assertEqual(
            set(queries),
            {
                "podMetrics",
                "nodeMetrics",
                "hostMetrics",
                "nodeCondition",
                "collectorSelfMetrics",
                "kubernetesEvent",
                "privacySafeApplicationLog",
                "forbiddenLogBody",
            },
        )
        for query in queries.values():
            self.assertIn("deployment.environment", query)
            self.assertIn("pre-dev", query)
            self.assertIn("k8s.cluster.name", query)
            self.assertIn("oriso-predev", query)

        self.assertIn("startsWith(metric_name, 'k8s.pod.')", queries["podMetrics"])
        self.assertIn("startsWith(metric_name, 'k8s.node.')", queries["nodeMetrics"])
        self.assertIn("startsWith(metric_name, 'system.')", queries["hostMetrics"])
        self.assertIn("metric_name = 'k8s.node.condition'", queries["nodeCondition"])
        self.assertIn(
            "startsWith(metric_name, 'otelcol_')", queries["collectorSelfMetrics"]
        )
        self.assertIn("oriso-k8s-infra", queries["collectorSelfMetrics"])
        self.assertIn("signoz_metrics.time_series_v4", queries["podMetrics"])
        self.assertIn("signoz_metrics.samples_v4", queries["podMetrics"])
        for metric_query in (
            "podMetrics",
            "nodeMetrics",
            "hostMetrics",
            "nodeCondition",
            "collectorSelfMetrics",
        ):
            self.assertIn("unix_milli", queries[metric_query])
            self.assertIn("INTERVAL 15 MINUTE", queries[metric_query])
        self.assertIn("signoz_logs.logs_v2", queries["kubernetesEvent"])
        self.assertIn("acceptance-1234", queries["kubernetesEvent"])
        self.assertIn(
            "[ORISO log body suppressed by privacy policy]",
            queries["privacySafeApplicationLog"],
        )
        self.assertIn(
            "0123456789abcdef0123456789abcdef", queries["privacySafeApplicationLog"]
        )
        self.assertIn("forbidden-marker-1234", queries["forbiddenLogBody"])

    def test_infra_readback_requires_positive_signals_and_zero_forbidden_body(
        self,
    ) -> None:
        healthy = {
            "podMetrics": 1,
            "nodeMetrics": 1,
            "hostMetrics": 1,
            "nodeCondition": 1,
            "collectorSelfMetrics": 1,
            "kubernetesEvent": 1,
            "privacySafeApplicationLog": 1,
            "forbiddenLogBody": 0,
        }

        self.assertEqual(MODULE.infra_readback_failures(healthy), [])
        healthy["hostMetrics"] = 0
        healthy["forbiddenLogBody"] = 1
        self.assertEqual(
            MODULE.infra_readback_failures(healthy),
            ["hostMetrics missing", "forbiddenLogBody leaked"],
        )

    def test_infra_rollouts_cover_node_and_cluster_collectors(self) -> None:
        self.assertEqual(
            MODULE.infra_rollout_targets("caritas"),
            (
                "daemonset/caritas-k8s-infra-otel-agent",
                "deployment/caritas-k8s-infra-otel-deployment",
            ),
        )


class RuntimeReadinessContractTest(unittest.TestCase):
    def statefulset(
        self, name: str, owner_uid: str = "current-chi", replicas: int = 1, deleting: bool = False,
    ) -> dict:
        return {
            "metadata": {
                "name": name,
                "uid": f"statefulset-{name}",
                "deletionTimestamp": "2026-11-07T12:00:00Z" if deleting else None,
                "ownerReferences": [{
                    "kind": "ClickHouseInstallation",
                    "name": "caritas-clickhouse",
                    "uid": owner_uid,
                    "controller": True,
                }],
            },
            "spec": {"replicas": replicas},
        }

    def runner(self, statefulsets: list[dict], failing_rollout: str = "") -> mock.Mock:
        runner = mock.Mock(spec=MODULE.Runner)

        def run(command: list[str]) -> subprocess.CompletedProcess:
            arguments = command[3:]
            output = ""
            if arguments == ["get", "chi", "caritas-clickhouse", "-o", "json"]:
                output = json.dumps({"metadata": {"uid": "current-chi"}})
            elif arguments == [
                "get", "statefulset", "-l", "clickhouse.altinity.com/chi=caritas-clickhouse", "-o", "json",
            ]:
                output = json.dumps({"items": statefulsets})
            elif failing_rollout and failing_rollout in arguments:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0, stdout=output)

        runner.run.side_effect = run
        return runner

    def checked_rollouts(self, runner: mock.Mock) -> list[str]:
        commands = [call.args[0][3:] for call in runner.run.call_args_list]
        return [command[2] for command in commands if command[:2] == ["rollout", "status"]]

    def test_checks_all_actual_statefulsets_owned_by_the_current_installation(self) -> None:
        runner = self.runner([
            self.statefulset("chi-caritas-clickhouse-cluster-1-0"),
            self.statefulset("orphaned-statefulset", owner_uid="previous-chi"),
            self.statefulset("chi-caritas-clickhouse-cluster-0-0"),
        ])
        MODULE._check_runtime_readiness(runner, "caritas", "caritas", "15s")
        self.assertEqual(self.checked_rollouts(runner), [
            "deployment/caritas-clickhouse-operator",
            "statefulset/chi-caritas-clickhouse-cluster-0-0",
            "statefulset/chi-caritas-clickhouse-cluster-1-0",
            "deployment/caritas-signoz-otel-collector",
            "statefulset/caritas-signoz",
        ])

    def test_missing_or_foreign_only_clickhouse_statefulsets_fail(self) -> None:
        for statefulsets in ([], [self.statefulset("orphaned-statefulset", owner_uid="previous-chi")]):
            with self.subTest(statefulsets=statefulsets):
                runner = self.runner(statefulsets)
                with self.assertRaisesRegex(RuntimeError, "no owned StatefulSets"):
                    MODULE._check_runtime_readiness(runner, "caritas", "caritas", "15s")

    def test_deleting_or_scaled_to_zero_statefulset_fails_even_if_another_is_ready(self) -> None:
        for statefulset in (
            self.statefulset("retiring-shard", deleting=True),
            self.statefulset("scaled-down-shard", replicas=0),
        ):
            with self.subTest(statefulset=statefulset):
                runner = self.runner([self.statefulset("active-shard"), statefulset])
                with self.assertRaisesRegex(RuntimeError, statefulset["metadata"]["name"]):
                    MODULE._check_runtime_readiness(runner, "caritas", "caritas", "15s")

    def test_failed_clickhouse_rollout_stops_before_collector_readiness(self) -> None:
        target = "statefulset/chi-caritas-clickhouse-cluster-0-0"
        runner = self.runner([self.statefulset("chi-caritas-clickhouse-cluster-0-0")], failing_rollout=target)
        with self.assertRaises(subprocess.CalledProcessError):
            MODULE._check_runtime_readiness(runner, "caritas", "caritas", "15s")
        self.assertEqual(self.checked_rollouts(runner), [
            "deployment/caritas-clickhouse-operator", target,
        ])


class CollectorPipelineContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "receivers": {"otlp": {"protocols": {"grpc": {}, "http": {}}}},
            "exporters": {
                "clickhousetraces": {},
                "signozclickhousemetrics": {},
                "clickhouselogsexporter": {},
            },
            "service": {
                "pipelines": {
                    "traces": {
                        "receivers": ["otlp"],
                        "exporters": ["clickhousetraces"],
                    },
                    "metrics": {
                        "receivers": ["otlp"],
                        "exporters": ["signozclickhousemetrics"],
                    },
                    "logs": {
                        "receivers": ["otlp"],
                        "exporters": ["clickhouselogsexporter"],
                    },
                }
            },
        }

    def test_accepts_complete_three_signal_pipeline(self) -> None:
        MODULE.validate_collector_config(self.config)

    def test_requires_both_otlp_protocols(self) -> None:
        for protocol in ("grpc", "http"):
            with self.subTest(protocol=protocol):
                config = deepcopy(self.config)
                del config["receivers"]["otlp"]["protocols"][protocol]
                with self.assertRaisesRegex(RuntimeError, protocol):
                    MODULE.validate_collector_config(config)

    def test_requires_otlp_receiver_in_every_signal_pipeline(self) -> None:
        for signal in ("traces", "metrics", "logs"):
            with self.subTest(signal=signal):
                config = deepcopy(self.config)
                config["service"]["pipelines"][signal]["receivers"] = []
                with self.assertRaisesRegex(RuntimeError, f"{signal}.*OTLP receiver"):
                    MODULE.validate_collector_config(config)

    def test_requires_declared_clickhouse_exporter_for_every_signal(self) -> None:
        required_exporters = {
            "traces": "clickhousetraces",
            "metrics": "signozclickhousemetrics",
            "logs": "clickhouselogsexporter",
        }
        for signal, exporter in required_exporters.items():
            with self.subTest(signal=signal):
                config = deepcopy(self.config)
                del config["exporters"][exporter]
                with self.assertRaisesRegex(RuntimeError, f"{signal}.*ClickHouse exporter"):
                    MODULE.validate_collector_config(config)

    def test_requires_clickhouse_exporter_in_every_signal_pipeline(self) -> None:
        for signal in ("traces", "metrics", "logs"):
            with self.subTest(signal=signal):
                config = deepcopy(self.config)
                config["service"]["pipelines"][signal]["exporters"] = []
                with self.assertRaisesRegex(RuntimeError, f"{signal}.*ClickHouse exporter"):
                    MODULE.validate_collector_config(config)


class CapacityContractTest(unittest.TestCase):
    minimum = 2 * 1024**3

    def setUp(self) -> None:
        self.argv = [
            "signoz_runtime_acceptance", "--namespace", "caritas", "--release", "caritas",
            "--environment", "dev", "--skip-k8s-infra",
        ]
        self.enterContext(mock.patch.object(MODULE.sys, "argv", self.argv))
        self.enterContext(mock.patch.object(MODULE, "_check_runtime_readiness"))
        self.enterContext(mock.patch.object(MODULE, "_check_collector_config"))
        self.emit = self.enterContext(mock.patch.object(MODULE, "_emit_signal"))
        self.retention = self.enterContext(mock.patch.object(MODULE, "_read_clickhouse_retention"))
        self.retention.side_effect = self.read_retention
        self.infra = self.enterContext(mock.patch.object(MODULE, "_emit_infra_probes", return_value=None))
        self.enterContext(mock.patch.object(MODULE, "_wait_for_infra_readback", return_value={}))
        self.enterContext(mock.patch.object(MODULE, "_read_java_signals", return_value={
            "missing": [], "traceIdentity": {},
        }))
        self.readback = self.enterContext(mock.patch.object(MODULE, "_wait_for_readback"))
        self.readback.return_value = {"traces": 1, "metrics": 1, "logs": 1}
        self.runner = self.enterContext(mock.patch.object(MODULE, "Runner")).return_value
        self.runner.run.side_effect = self.run_kubectl
        self.spaces = {"clickhouse-b": str(self.minimum + 1), "clickhouse-a": str(self.minimum)}
        self.not_ready = set()
        self.disks_checked = []
        self.include_orphan = False
        self.retention_checked = []
        self.retention_failures = dict.fromkeys(self.spaces, ())

    def select_full_scope(self) -> None:
        self.argv.remove("--skip-k8s-infra")
        self.argv.extend(["--cluster-name", "oriso-dev"])

    def read_retention(self, runner, namespace: str, pod: str) -> dict:
        self.assertIs(runner, self.runner)
        self.assertEqual(namespace, "caritas")
        self.assertEqual(self.disks_checked, [])
        self.retention_checked.append(pod)
        return {"failures": list(self.retention_failures[pod])}

    def run_kubectl(self, command: list[str]) -> mock.Mock:
        if command[3:] == ["get", "chi", "caritas-clickhouse", "-o", "json"]:
            return mock.Mock(stdout=json.dumps({"metadata": {"uid": "current-chi"}}))
        if command[3:] == [
            "get", "statefulset", "-l", "clickhouse.altinity.com/chi=caritas-clickhouse", "-o", "json",
        ]:
            return mock.Mock(stdout=json.dumps({"items": [
                {
                    "metadata": {
                        "name": f"statefulset-{name}",
                        "uid": f"statefulset-{name}",
                        "ownerReferences": [{
                            "kind": "ClickHouseInstallation",
                            "name": "caritas-clickhouse",
                            "uid": "current-chi",
                            "controller": True,
                        }],
                    },
                    "spec": {"replicas": 1},
                }
                for name in self.spaces
            ]}))
        if command[3:5] == ["get", "pods"]:
            self.assertIn("clickhouse.altinity.com/chi=caritas-clickhouse", command[6])
            items = [
                {
                    "metadata": {
                        "name": name,
                        "ownerReferences": [{
                            "kind": "StatefulSet",
                            "uid": f"statefulset-{name}",
                            "controller": True,
                        }],
                    },
                    "status": {"conditions": [{
                        "type": "Ready", "status": "False" if name in self.not_ready else "True",
                    }]},
                }
                for name in self.spaces
            ]
            if self.include_orphan:
                items.append({
                    "metadata": {
                        "name": "orphaned-clickhouse",
                        "ownerReferences": [{
                            "kind": "StatefulSet", "uid": "previous-statefulset", "controller": True,
                        }],
                    },
                    "status": {"conditions": [{"type": "Ready", "status": "False"}]},
                })
            return mock.Mock(stdout=json.dumps({"items": items}))
        self.assertEqual(command[3], "exec")
        self.assertIn("unreserved_space", command[-1])
        pod = command[4]
        self.disks_checked.append(pod)
        return mock.Mock(stdout=self.spaces[pod])

    def run_gate(self) -> dict:
        output = io.StringIO()
        with redirect_stdout(output):
            MODULE.main()
        return json.loads(output.getvalue())

    def test_every_data_pod_is_checked_before_writing_and_exact_limit_passes(self) -> None:
        def assert_preflight(*_args):
            self.assertEqual(self.disks_checked, ["clickhouse-a", "clickhouse-b"])

        self.emit.side_effect = assert_preflight
        report = self.run_gate()

        self.assertEqual(report["clickhouseCapacity"], "passed")
        self.assertEqual(report["clickhouseRetention"], "skipped")
        self.retention.assert_not_called()
        self.assertEqual(self.emit.call_count, 3)
        self.assertEqual(self.readback.call_args.args[2], "clickhouse-a")

    def test_full_scope_checks_retention_and_headroom_on_every_pod_before_writing(self) -> None:
        self.select_full_scope()
        pods = ["clickhouse-a", "clickhouse-b"]

        def assert_preflight(*_args):
            self.assertEqual(self.retention_checked, pods)
            self.assertEqual(self.disks_checked, pods)

        self.emit.side_effect = assert_preflight
        report = self.run_gate()
        self.assertEqual(report["clickhouseRetention"], {
            "status": "passed", "pods": dict.fromkeys(pods, {"failures": []}),
        })
        self.assertEqual(report["clickhouseCapacity"], "passed")
        self.assertEqual(self.emit.call_count, 3)
        self.infra.assert_called_once()

    def test_full_retention_failure_stops_before_any_probe(self) -> None:
        self.select_full_scope()
        self.retention_failures["clickhouse-b"] = ("trace_log has a 7-day deletion TTL.",)
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaisesRegex(
            RuntimeError, "ClickHouse retention conformance failed on clickhouse-b",
        ):
            MODULE.main()

        report = json.loads(output.getvalue())
        self.assertEqual(report["environment"], "dev")
        self.assertEqual(report["clickhouseCapacity"], "not_reached")
        self.assertEqual(report["clickhouseRetention"], {
            "status": "failed",
            "pods": {
                "clickhouse-a": {"failures": []},
                "clickhouse-b": {"failures": ["trace_log has a 7-day deletion TTL."]},
            },
        })
        self.emit.assert_not_called()
        self.infra.assert_not_called()
        self.readback.assert_not_called()

    def test_full_scope_still_requires_headroom_after_retention_passes(self) -> None:
        self.select_full_scope()
        self.spaces["clickhouse-b"] = str(self.minimum - 1)
        with self.assertRaisesRegex(RuntimeError, "clickhouse-b has 2147483647 unreserved bytes"):
            self.run_gate()

        self.assertEqual(self.retention_checked, ["clickhouse-a", "clickhouse-b"])
        self.emit.assert_not_called()
        self.infra.assert_not_called()

    def test_unreadable_full_retention_stops_before_any_probe(self) -> None:
        self.select_full_scope()
        self.retention.side_effect = RuntimeError("ClickHouse retention metadata unreadable")
        with self.assertRaisesRegex(RuntimeError, "retention metadata unreadable"):
            self.run_gate()

        self.emit.assert_not_called()
        self.infra.assert_not_called()

    def test_low_capacity_on_another_data_pod_stops_writes(self) -> None:
        self.spaces["clickhouse-b"] = str(self.minimum - 1)

        with self.assertRaisesRegex(RuntimeError, "clickhouse-b has 2147483647 unreserved bytes"):
            self.run_gate()

        self.emit.assert_not_called()
        self.readback.assert_not_called()

    def test_unreadable_capacity_stops_writes(self) -> None:
        for invalid in ("", "unknown", "2147483648\n2147483648", str(2**64 - 1)):
            with self.subTest(invalid=invalid):
                self.spaces["clickhouse-a"] = invalid
                with self.assertRaisesRegex(RuntimeError, "capacity unreadable.*clickhouse-a"):
                    self.run_gate()

        self.emit.assert_not_called()

    def test_unready_data_pod_stops_writes(self) -> None:
        self.not_ready.add("clickhouse-b")

        with self.assertRaisesRegex(RuntimeError, "clickhouse-b is not ready"):
            self.run_gate()

        self.emit.assert_not_called()

    def test_orphaned_clickhouse_pod_does_not_block_the_current_installation(self) -> None:
        self.include_orphan = True

        report = self.run_gate()

        self.assertEqual(report["clickhouseCapacity"], "passed")
        self.assertEqual(self.disks_checked, ["clickhouse-a", "clickhouse-b"])

    def test_readiness_only_skips_capacity_and_writes(self) -> None:
        self.select_full_scope()
        self.argv.append("--skip-synthetic")

        report = self.run_gate()
        self.assertEqual(report["clickhouseCapacity"], "skipped")
        self.assertEqual(report["clickhouseRetention"], "skipped")

        self.retention.assert_not_called()
        self.runner.run.assert_not_called()
        self.emit.assert_not_called()

    def test_capacity_floor_matches_the_managed_alert(self) -> None:
        catalog = SCRIPT.parents[1] / "files/signoz/observability-catalog.json"
        alerts = json.loads(catalog.read_text(encoding="utf-8"))["alerts"]
        capacity = next(alert for alert in alerts if alert["slug"] == "clickhouse-capacity")
        self.assertEqual(MODULE.MIN_CLICKHOUSE_UNRESERVED_BYTES, capacity["target"])


class JavaSignalContractTest(unittest.TestCase):
    services = ("user-service", "agency-service", "tenant-service", "consulting-type-service")
    signals = ("logs", "metrics", "traces", "logToTrace")

    def setUp(self) -> None:
        self.rows = [
            {"service_name": service, "signal": signal, "samples": "1"}
            for service in self.services for signal in self.signals
        ]
        for row in self.rows:
            if row["signal"] == "traces":
                row.update(wrong_environment="0", invalid_version=0)
        self.runner = self.enterContext(mock.patch.object(MODULE, "Runner")).return_value
        self.runner.run.side_effect = self.run_query
        self.enterContext(mock.patch.object(MODULE, "_clickhouse_pods", return_value=["data-pod"]))

    def run_query(self, command: list[str]) -> mock.Mock:
        self.assertEqual(command[:7], ["kubectl", "-n", "caritas", "exec", "data-pod", "-c", "clickhouse"])
        self.assertIn("JSONEachRow", command)
        sql = command[-1]
        self.assertIn("signoz_metrics.distributed_samples_v4", sql)
        self.assertIn("(service_name, trace_id, span_id) GLOBAL IN", sql)
        self.assertIn("resources_string['deployment.environment'] = 'dev'", sql)
        return mock.Mock(stdout="\n".join(json.dumps(row) for row in self.rows))

    def run_gate(self, *flags: str, failure: str | None = None) -> dict:
        argv = ["signoz_runtime_acceptance", "--namespace", "caritas", "--release", "caritas",
                "--environment", "dev", *flags]
        output = io.StringIO()
        with mock.patch.object(MODULE.sys, "argv", argv), redirect_stdout(output):
            if failure is None:
                MODULE.main()
            else:
                with self.assertRaisesRegex(RuntimeError, failure):
                    MODULE.main()
        return json.loads(output.getvalue())

    def test_java_signal_diagnosis_reads_four_services_without_a_probe(self) -> None:
        report = self.run_gate("--check-java-signals")

        self.assertEqual(report["missing"], [])
        self.assertEqual(report["services"]["user-service"], dict.fromkeys(self.signals, 1))
        self.assertEqual(report["traceIdentity"]["user-service"], {
            "wrongEnvironment": 0, "invalidVersion": 0,
        })
        self.assertEqual((report["freshnessMinutes"], report["correlationMinutes"]), (15, 60))
        self.runner.run.assert_called_once()

    def test_java_signal_diagnosis_fails_with_the_exact_missing_pair(self) -> None:
        self.rows = [row for row in self.rows if (
            row["service_name"], row["signal"]
        ) != ("tenant-service", "logToTrace")]

        with self.assertRaisesRegex(RuntimeError, "tenant-service/logToTrace"):
            self.run_gate("--check-java-signals")

    def test_java_signal_diagnosis_rejects_ambiguous_counts(self) -> None:
        self.rows.append(self.rows[0])

        with self.assertRaisesRegex(RuntimeError, "invalid Java signal readback"):
            self.run_gate("--check-java-signals")

    def test_java_signal_diagnosis_fails_when_fresh_traces_have_bad_identity(self) -> None:
        trace = next(row for row in self.rows if row["signal"] == "traces")
        trace.update(samples="2", wrong_environment="1", invalid_version=1)
        report = self.run_gate("--check-java-signals", failure="Java trace identity.*user-service")

        self.assertEqual(report["missing"], [])
        self.assertEqual(report["services"]["user-service"]["traces"], 2)
        self.assertEqual(report["traceIdentity"]["user-service"], {
            "wrongEnvironment": 1, "invalidVersion": 1,
        })

    def test_java_signal_diagnosis_requires_both_trace_identity_counts(self) -> None:
        trace = next(row for row in self.rows if row["signal"] == "traces")
        for key in ("wrong_environment", "invalid_version"):
            with self.subTest(key=key):
                original = trace.pop(key)
                with self.assertRaisesRegex(RuntimeError, "invalid Java signal readback"):
                    self.run_gate("--check-java-signals")
                trace[key] = original

    def test_java_signal_diagnosis_rejects_impossible_trace_identity_counts(self) -> None:
        trace = next(row for row in self.rows if row["signal"] == "traces")
        for invalid in (2, -1, True, "unknown"):
            with self.subTest(invalid=invalid):
                trace["invalid_version"] = invalid
                with self.assertRaisesRegex(RuntimeError, "invalid Java signal readback"):
                    self.run_gate("--check-java-signals")

    def test_java_signal_diagnosis_treats_overlapping_violations_as_valid_counts(self) -> None:
        trace = next(row for row in self.rows if row["signal"] == "traces")
        trace.update(wrong_environment=1, invalid_version=1)

        report = self.run_gate("--check-java-signals", failure="Java trace identity.*user-service")

        self.assertEqual(report["traceIdentity"]["user-service"], {
            "wrongEnvironment": 1, "invalidVersion": 1,
        })

    def test_full_acceptance_requires_java_evidence_after_probe_cleanup(self) -> None:
        self.enterContext(mock.patch.object(MODULE, "_read_clickhouse_retention", return_value={"failures": []}))
        for method in ("_check_runtime_readiness", "_check_collector_config", "_check_clickhouse_capacity",
                       "_emit_signal", "_wait_for_readback", "_wait_for_infra_readback"):
            self.enterContext(mock.patch.object(MODULE, method, return_value={}))
        self.enterContext(mock.patch.object(MODULE, "_emit_infra_probes", return_value="log-probe"))
        deleted = self.enterContext(mock.patch.object(MODULE, "_delete_probe_pod"))
        complete_rows = self.rows
        self.rows = [row for row in self.rows if row["signal"] != "logToTrace"]

        def query_after_cleanup(command: list[str]) -> mock.Mock:
            deleted.assert_called_once_with(self.runner, "caritas", "log-probe")
            return self.run_query(command)

        self.runner.run.side_effect = query_after_cleanup

        with self.assertRaisesRegex(RuntimeError, "user-service/logToTrace"):
            self.run_gate("--cluster-name", "oriso-dev")

        deleted.reset_mock()
        self.rows = complete_rows
        trace = next(row for row in self.rows if row["signal"] == "traces")
        trace["invalid_version"] = 1
        with self.assertRaisesRegex(RuntimeError, "Java trace identity.*user-service"):
            self.run_gate("--cluster-name", "oriso-dev")


class ClickHouseRetentionContractTest(unittest.TestCase):
    log_budget = 10 * 1024**3
    disk_size = 40 * 1024**3
    log_tables = (
        "query_log", "text_log", "trace_log", "metric_log", "asynchronous_metric_log",
        "part_log", "zookeeper_log", "aggregated_zookeeper_log", "processors_profile_log",
    )
    engine = "MergeTree PARTITION BY event_date ORDER BY event_time"

    def setUp(self) -> None:
        self.disks = {
            pod: [{"type": "Local", "total_space": str(self.disk_size), "free_space": self.disk_size // 4}]
            for pod in ("clickhouse-b", "clickhouse-a")
        }
        self.tables = [
            {"name": name, "engine_full": self.engine + " TTL event_date + toIntervalDay(3)"}
            for name in self.log_tables
        ]
        self.parts = [
            {"table": "query_log_4", "active_bytes": str(self.log_budget - 1)},
            {"table": "trace_log", "active_bytes": 1},
        ]
        runner = self.enterContext(mock.patch.object(MODULE, "Runner")).return_value
        runner.run.side_effect = self.run_kubectl
        self.checked_disks = []
        self.writer_snapshots = {
            pod: [f"{'a' * 64}\tactive\tactive"] * 2 for pod in self.disks
        }

    def run_kubectl(self, command: list[str], *, input_text: str | None = None) -> mock.Mock:
        if command[3:5] == ["get", "pods"]:
            self.assertIn("clickhouse.altinity.com/chi=caritas-clickhouse", command[6])
            return mock.Mock(stdout=json.dumps({"items": [
                {"metadata": {"name": pod}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
                for pod in self.disks
            ]}))
        if command[7:11] == ["-i", "--", "sh", "-s"]:
            self.assertIn("MainConfigLoads", input_text)
            pod = command[4]
            index = self.writer_reads[pod]
            self.writer_reads[pod] += 1
            return mock.Mock(stdout=self.writer_snapshots[pod][index])
        self.assertEqual(command[3:9], [
            "exec", command[4], "-c", "clickhouse", "--", "clickhouse-client",
        ])
        self.assertIn("JSONEachRow", command)
        self.assertIn("--readonly=1", command)
        self.assertIn("--log_queries=0", command)
        self.assertIn("127.0.0.1", command)
        sql = command[-1]
        self.assertTrue(sql.startswith("SELECT "))
        if "FROM system.disks" in sql:
            self.assertIn("WHERE name = 'default'", sql)
            self.checked_disks.append(command[4])
            rows = self.disks[command[4]]
        elif "FROM system.tables" in sql:
            self.assertIn("engine_full", sql)
            self.assertIn("database = 'system'", sql)
            rows = self.tables
        else:
            self.assertIn("FROM system.parts", sql)
            self.assertIn("WHERE active AND database = 'system'", sql)
            self.assertIn("match(table, '^(" + "|".join(self.log_tables) + ")(_[0-9]+)?$')", sql)
            rows = self.parts
        return mock.Mock(stdout="\n".join(json.dumps(row) for row in rows))

    def run_gate(self, *, failed: bool = False) -> dict:
        self.writer_reads = dict.fromkeys(self.disks, 0)
        argv = [
            "signoz_runtime_acceptance", "--namespace", "caritas", "--release", "caritas",
            "--environment", "dev", "--check-clickhouse-retention",
        ]
        output = io.StringIO()
        with mock.patch.object(MODULE.sys, "argv", argv), redirect_stdout(output):
            if failed:
                with self.assertRaisesRegex(RuntimeError, "retention conformance failed"):
                    MODULE.main()
            else:
                MODULE.main()
        return json.loads(output.getvalue())

    def test_checks_both_pods_and_accepts_exact_limits(self) -> None:
        self.tables[0]["engine_full"] += " SETTINGS index_granularity = 8192"
        report = self.run_gate()

        self.assertEqual(self.checked_disks, ["clickhouse-a", "clickhouse-b"])
        pod = report["pods"]["clickhouse-a"]
        self.assertEqual(pod["deletionTtlDays"], dict.fromkeys(self.log_tables, 3))
        self.assertEqual(pod["diskUsedPercent"], 75)
        self.assertEqual(pod["trackedLogBytes"], self.log_budget)
        self.assertEqual(pod["rotatedLogBytes"], self.log_budget - 1)
        self.assertEqual(pod["failures"], [])
        self.assertEqual(pod["logWriters"], {"metric_log": "active", "trace_log": "active"})

    def test_compares_exact_bytes_and_reports_both_exceeded_limits(self) -> None:
        self.disks["clickhouse-b"][0]["free_space"] -= 1
        self.parts[1]["active_bytes"] += 1
        report = self.run_gate(failed=True)

        self.assertEqual(report["pods"]["clickhouse-a"]["trackedLogBytes"], self.log_budget + 1)
        self.assertEqual(len(report["pods"]["clickhouse-a"]["failures"]), 1)
        self.assertEqual(len(report["pods"]["clickhouse-b"]["failures"]), 2)
        self.assertEqual(report["pods"]["clickhouse-b"]["diskFreeBytes"], self.disk_size // 4 - 1)

    def test_counts_all_nine_families_including_rotations(self) -> None:
        self.parts = [
            {"table": name, "active_bytes": 10} for name in self.log_tables
        ] + [{"table": "zookeeper_log_7", "active_bytes": 15}]
        pod = self.run_gate()["pods"]["clickhouse-a"]
        self.assertEqual(pod["trackedLogBytes"], 105)
        self.assertEqual(pod["rotatedLogBytes"], 15)
        self.assertEqual(pod["activeLogBytesByTable"]["aggregated_zookeeper_log"], 10)
        self.assertEqual(pod["activeLogBytesByTable"]["processors_profile_log"], 10)

    def test_exempts_only_proven_disabled_writers_but_counts_all_of_their_bytes(self) -> None:
        for pod in self.writer_snapshots:
            self.writer_snapshots[pod] = [f"{'b' * 64}\tdisabled\tdisabled"] * 2
        self.tables = [row for row in self.tables if row["name"] != "metric_log"]
        trace = next(row for row in self.tables if row["name"] == "trace_log")
        trace["engine_full"] = self.engine + " TTL event_date + toIntervalDay(7)"
        self.parts = [
            {"table": "trace_log", "active_bytes": 1024},
            {"table": "metric_log_8", "active_bytes": self.log_budget - 1024},
        ]
        pod = self.run_gate()["pods"]["clickhouse-a"]
        self.assertEqual(pod["logWriters"], {"metric_log": "disabled", "trace_log": "disabled"})
        self.assertEqual(pod["deletionTtlDays"]["trace_log"], 7)
        self.assertEqual(pod["trackedLogBytes"], self.log_budget)
        self.assertEqual(pod["rotatedLogBytes"], self.log_budget - 1024)
        self.assertEqual(pod["retainedDisabledLogBytes"], self.log_budget)

        self.parts[0]["active_bytes"] += 1
        failed = self.run_gate(failed=True)["pods"]["clickhouse-a"]
        self.assertEqual(len(failed["failures"]), 1)
        self.assertIn("Active system log parts use", failed["failures"][0])

    def test_unknown_or_changed_writer_evidence_keeps_canonical_ttl_required(self) -> None:
        for snapshots in (
            ["unverified", "unverified"],
            [f"{'a' * 64}\tdisabled\tdisabled", f"{'b' * 64}\tdisabled\tdisabled"],
            [f"{'a' * 64}\tdisabled\tunverified"] * 2,
        ):
            with self.subTest(snapshots=snapshots):
                self.writer_snapshots["clickhouse-a"] = snapshots
                trace = next(row for row in self.tables if row["name"] == "trace_log")
                trace["engine_full"] = self.engine + " TTL event_date + toIntervalDay(7)"
                pod = self.run_gate(failed=True)["pods"]["clickhouse-a"]
                self.assertEqual(pod["logWriters"]["trace_log"], "unverified")
                self.assertIn("trace_log", pod["failures"][0])
                self.assertEqual(pod["trackedLogBytes"], self.log_budget)

    def test_missing_canonical_table_fails_even_when_a_rotation_exists(self) -> None:
        self.tables.pop(0)
        pod = self.run_gate(failed=True)["pods"]["clickhouse-a"]
        self.assertIn("query_log", pod["failures"][0])
        self.assertEqual(pod["rotatedLogBytes"], self.log_budget - 1)

    def test_requires_the_exact_unconditional_deletion_ttl(self) -> None:
        for ttl in (
            " TTL event_date + toIntervalDay(7)",
            " TTL event_date + toIntervalDay(3) TO VOLUME 'slow'",
            " TTL event_date + toIntervalDay(3) DELETE WHERE event_time > now()",
            " TTL event_date + toIntervalDay(3), event_date + toIntervalDay(4) TO VOLUME 'slow'",
            " SETTINGS storage_policy = ' TTL event_date + toIntervalDay(3) '",
        ):
            with self.subTest(ttl=ttl):
                self.tables[0]["engine_full"] = self.engine + ttl
                pod = self.run_gate(failed=True)["pods"]["clickhouse-a"]
                self.assertIn("query_log", pod["failures"][0])

    def test_rejects_missing_duplicate_and_invalid_local_disk_readback(self) -> None:
        disk = self.disks["clickhouse-a"][0]
        for rows in (
            [], [disk, disk], [dict(disk, type="ObjectStorage")], [dict(disk, total_space=0)],
            [dict(disk, free_space=self.disk_size + 1)], [dict(disk, free_space=str(2**64 - 1))],
        ):
            with self.subTest(rows=rows):
                self.disks["clickhouse-a"] = rows
                with self.assertRaisesRegex(RuntimeError, "retention metadata unreadable on pod clickhouse-a"):
                    self.run_gate()

    def test_rejects_invalid_table_and_part_readback(self) -> None:
        cases = (
            (self.tables, self.tables[0]),
            (self.parts, self.parts[0]),
            (self.parts, {"table": "zookeeper_log_foreign", "active_bytes": 1}),
            (self.parts, {"table": "text_log_8", "active_bytes": True}),
        )
        for rows, bad in cases:
            with self.subTest(bad=bad):
                rows.append(bad)
                try:
                    with self.assertRaisesRegex(RuntimeError, "retention metadata unreadable on pod clickhouse-a"):
                        self.run_gate()
                finally:
                    rows.pop()


class RunnerContractTest(unittest.TestCase):
    @mock.patch.object(MODULE.subprocess, "run")
    def test_local_runner_passes_command_once_to_subprocess(
        self, run: mock.Mock
    ) -> None:
        run.return_value = mock.Mock(stdout="ok")

        MODULE.Runner().run(["kubectl", "version"])

        run.assert_called_once()
        positional, keyword = run.call_args
        self.assertEqual(positional, (["kubectl", "version"],))
        self.assertTrue(keyword["check"])

    def test_infra_probe_emits_namespaced_event_and_delayed_json_log(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = MODULE.subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        pod_name = MODULE._emit_infra_probes(
            runner,
            "caritas",
            "acceptance-12345678",
            "0123456789abcdef0123456789abcdef",
            "0123456789abcdef",
            "forbidden-marker-1234",
        )

        self.assertEqual(pod_name, "signoz-log-acceptance-12345678")
        commands = [call.args[0] for call in runner.run.call_args_list]
        self.assertIn("create", commands[0])
        self.assertIn("event", commands[0])
        self.assertIn("--for=namespace/caritas", commands[0])
        self.assertIn("run", commands[1])
        self.assertIn("sleep 5; printf '%s\\n' \"$1\"; sleep 10", commands[1])
        self.assertTrue(any("forbidden-marker-1234" in arg for arg in commands[1]))
        self.assertIn("wait", commands[2])

    def test_infra_probe_pod_is_deleted_when_the_probe_fails(self) -> None:
        """The documented cleanup must run on the failure branch, not only on success."""
        runner = mock.Mock()
        ok = MODULE.subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        def _run(command, *_args, **_kwargs):
            if "run" in command:
                raise MODULE.subprocess.CalledProcessError(1, command)
            return ok

        runner.run.side_effect = _run

        with self.assertRaises(MODULE.subprocess.CalledProcessError):
            MODULE._emit_infra_probes(
                runner,
                "caritas",
                "acceptance-12345678",
                "0123456789abcdef0123456789abcdef",
                "0123456789abcdef",
                "forbidden-marker-1234",
            )

        delete_commands = [
            call.args[0] for call in runner.run.call_args_list if "delete" in call.args[0]
        ]
        self.assertEqual(len(delete_commands), 1)
        self.assertEqual(
            delete_commands[0],
            [
                "kubectl",
                "-n",
                "caritas",
                "delete",
                "pod",
                "signoz-log-acceptance-12345678",
                "--ignore-not-found",
                "--wait=false",
            ],
        )

    def test_infra_probe_pod_is_deleted_when_the_readiness_wait_fails(self) -> None:
        runner = mock.Mock()
        ok = MODULE.subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        def _run(command, *_args, **_kwargs):
            if "wait" in command:
                raise MODULE.subprocess.CalledProcessError(1, command)
            return ok

        runner.run.side_effect = _run

        with self.assertRaises(MODULE.subprocess.CalledProcessError):
            MODULE._emit_infra_probes(
                runner,
                "caritas",
                "acceptance-12345678",
                "0123456789abcdef0123456789abcdef",
                "0123456789abcdef",
                "forbidden-marker-1234",
            )

        self.assertTrue(
            any("delete" in call.args[0] for call in runner.run.call_args_list),
            "a timed-out probe pod must still be removed",
        )


if __name__ == "__main__":
    unittest.main()
