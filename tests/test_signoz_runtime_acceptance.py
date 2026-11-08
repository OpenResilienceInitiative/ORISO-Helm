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
        self.readback = self.enterContext(mock.patch.object(MODULE, "_wait_for_readback"))
        self.readback.return_value = {"traces": 1, "metrics": 1, "logs": 1}
        self.runner = self.enterContext(mock.patch.object(MODULE, "Runner")).return_value
        self.runner.run.side_effect = self.run_kubectl
        self.spaces = {"clickhouse-b": str(self.minimum + 1), "clickhouse-a": str(self.minimum)}
        self.not_ready = set()
        self.disks_checked = []
        self.include_orphan = False

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
        self.assertEqual(self.emit.call_count, 3)
        self.assertEqual(self.readback.call_args.args[2], "clickhouse-a")

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
        self.argv.append("--skip-synthetic")

        self.assertEqual(self.run_gate()["clickhouseCapacity"], "skipped")

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

    def run_gate(self, *flags: str) -> dict:
        argv = ["signoz_runtime_acceptance", "--namespace", "caritas", "--release", "caritas",
                "--environment", "dev", *flags]
        output = io.StringIO()
        with mock.patch.object(MODULE.sys, "argv", argv), redirect_stdout(output):
            MODULE.main()
        return json.loads(output.getvalue())

    def test_java_signal_diagnosis_reads_four_services_without_a_probe(self) -> None:
        report = self.run_gate("--check-java-signals")

        self.assertEqual(report["missing"], [])
        self.assertEqual(report["services"]["user-service"], dict.fromkeys(self.signals, 1))
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

    def test_full_acceptance_requires_java_evidence_after_probe_cleanup(self) -> None:
        for method in ("_check_runtime_readiness", "_check_collector_config", "_check_clickhouse_capacity",
                       "_emit_signal", "_wait_for_readback", "_wait_for_infra_readback"):
            self.enterContext(mock.patch.object(MODULE, method, return_value={}))
        self.enterContext(mock.patch.object(MODULE, "_emit_infra_probes", return_value="log-probe"))
        deleted = self.enterContext(mock.patch.object(MODULE, "_delete_probe_pod"))
        self.rows = [row for row in self.rows if row["signal"] != "logToTrace"]

        def query_after_cleanup(command: list[str]) -> mock.Mock:
            deleted.assert_called_once_with(self.runner, "caritas", "log-probe")
            return self.run_query(command)

        self.runner.run.side_effect = query_after_cleanup

        with self.assertRaisesRegex(RuntimeError, "user-service/logToTrace"):
            self.run_gate("--cluster-name", "oriso-dev")


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
