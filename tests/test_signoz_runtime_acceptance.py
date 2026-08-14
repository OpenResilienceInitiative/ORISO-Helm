#!/usr/bin/env python3
"""Unit contracts for the executable SigNoz runtime acceptance gate."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


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
        self.assertIn('"service.name", "value": {"stringValue": "oriso-signoz-acceptance"}', encoded)
        self.assertIn('"deployment.environment", "value": {"stringValue": "predev"}', encoded)
        self.assertIn('"oriso.acceptance.id", "value": {"stringValue": "acceptance-1234"}', encoded)
        self.assertIn('"name": "oriso.signoz.acceptance"', encoded)
        self.assertIn('"traceId": "0123456789abcdef0123456789abcdef"', encoded)
        self.assertIn('"spanId": "0123456789abcdef"', encoded)

        forbidden = ("email", "password", "token", "message.body", "username")
        for name in forbidden:
            self.assertNotIn(name, encoded.lower())


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
        self.assertIn("signoz_logs.logs_v2", queries["logs"])


class CollectorPipelineContractTest(unittest.TestCase):
    def test_requires_otlp_receiver_and_clickhouse_exporter_per_signal(self) -> None:
        config = {
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

        MODULE.validate_collector_config(config)
        config["service"]["pipelines"]["logs"]["exporters"] = []
        with self.assertRaisesRegex(RuntimeError, "logs.*ClickHouse exporter"):
            MODULE.validate_collector_config(config)


if __name__ == "__main__":
    unittest.main()
