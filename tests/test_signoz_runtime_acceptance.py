#!/usr/bin/env python3
"""Unit contracts for the executable SigNoz runtime acceptance gate."""

from __future__ import annotations

import importlib.util
import json
import unittest
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


class RunnerContractTest(unittest.TestCase):
    @mock.patch.object(MODULE.subprocess, "run")
    def test_local_runner_passes_command_once_to_subprocess(
        self, run: mock.Mock
    ) -> None:
        run.return_value = mock.Mock(stdout="ok")

        MODULE.Runner().run(["kubectl", "version"])

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


if __name__ == "__main__":
    unittest.main()
