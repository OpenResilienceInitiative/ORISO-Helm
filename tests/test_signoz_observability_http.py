#!/usr/bin/env python3
"""End-to-end HTTP contract for the SigNoz provision/check client."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "signoz_observability", ROOT / "files" / "signoz" / "signoz_observability.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class State:
    def __init__(self) -> None:
        # Made by hand on the real SigNoz; the program must never write it.
        self.channels: dict[str, dict[str, Any]] = {
            "channel-1": {
                "id": "channel-1",
                "name": "oriso-alerts-slack",
                "type": "slack",
            }
        }
        self.channel_writes = 0
        self.test_rules: list[dict[str, Any]] = []
        self.test_rule_alert_count = 1
        self.dashboards: dict[str, dict[str, Any]] = {}
        self.rules: dict[str, dict[str, Any]] = {}
        self.queries = 0
        self.query_results: dict[str, list[dict[str, Any]]] = {}
        self.trace_responses: dict[str, dict[str, Any]] = {}


class Handler(BaseHTTPRequestHandler):
    state: State

    def log_message(self, *_: object) -> None:
        pass

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length)) if length else {}

    def _send(self, status: int, data: Any = None) -> None:
        self.send_response(status)
        if data is None:
            self.end_headers()
            return
        encoded = json.dumps({"status": "success", "data": data}).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _require_key(self) -> bool:
        if self.headers.get("SIGNOZ-API-KEY") == "test-api-key":
            return True
        self._send(403, {"error": "forbidden"})
        return False

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._require_key():
            return
        if self.path == "/api/v1/channels":
            self._send(200, list(self.state.channels.values()))
        elif self.path == "/api/v2/dashboards":
            items = [
                {"id": key, "name": value["name"]}
                for key, value in self.state.dashboards.items()
            ]
            self._send(200, {"dashboards": items})
        elif self.path.startswith("/api/v2/dashboards/"):
            self._send(200, self.state.dashboards[self.path.rsplit("/", 1)[1]])
        elif self.path == "/api/v2/rules":
            self._send(200, list(self.state.rules.values()))
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._require_key():
            return
        body = self._body()
        if self.path.startswith("/api/v1/channels"):
            self.state.channel_writes += 1
            self._send(405, {"error": "channel writes are not allowed"})
        elif self.path == "/api/v1/testRule":
            self.state.test_rules.append(body)
            self._send(
                200,
                {
                    "alertCount": self.state.test_rule_alert_count,
                    "message": "notification sent",
                },
            )
        elif self.path == "/api/v2/dashboards":
            identifier = f"dashboard-{len(self.state.dashboards) + 1}"
            self.state.dashboards[identifier] = {"id": identifier, **body}
            self._send(201, self.state.dashboards[identifier])
        elif self.path == "/api/v2/rules":
            identifier = f"rule-{len(self.state.rules) + 1}"
            self.state.rules[identifier] = {"id": identifier, **body}
            self._send(201, self.state.rules[identifier])
        elif self.path == "/api/v5/query_range":
            self.state.queries += 1
            query = body["compositeQuery"]["queries"][0]["spec"]
            if query["signal"] == "traces":
                alias = query["aggregations"][0].get("alias", "")
                self._send(200, self.state.trace_responses.get(alias, {
                    "type": "time_series",
                    "data": {"results": [{"queryName": query["name"], "aggregations": None}]},
                }))
                return
            expression = query["filter"]["expression"]
            metric = query["aggregations"][0]["metricName"]
            aggregations: list[dict[str, Any]] = []
            if "deployment.environment = 'pre-dev'" in expression and "outcome = 'partial_failure'" not in expression:
                aggregations = [{
                    "index": 0, "series": [{
                        "labels": [
                            {"key": {"name": field["name"]}, "value": "fixture"}
                            for field in query.get("groupBy", [])
                        ],
                        "values": [{
                            "timestamp": body["end"] - 60_000,
                            "value": 0 if metric == "oriso.live_chat.queue.visibility" else 1,
                        }],
                    }],
                }]
            results = self.state.query_results.get(
                metric, [{"queryName": query["name"], "aggregations": aggregations}]
            )
            self._send(200, {"type": "time_series", "data": {"results": results}})
        else:
            self._send(404, {"error": "not found"})

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._require_key():
            return
        body = self._body()
        identifier = self.path.rsplit("/", 1)[1]
        if self.path.startswith("/api/v1/channels"):
            self.state.channel_writes += 1
            self._send(405, {"error": "channel writes are not allowed"})
        elif self.path.startswith("/api/v2/dashboards/"):
            self.state.dashboards[identifier] = {"id": identifier, **body}
            self._send(200, self.state.dashboards[identifier])
        elif self.path.startswith("/api/v2/rules/"):
            self.state.rules[identifier] = {"id": identifier, **body}
            self._send(204)
        else:
            self._send(404, {"error": "not found"})


class HttpFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = State()
        handler = type("BoundHandler", (Handler,), {"state": self.state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _main(self, *argv: str) -> tuple[int, str]:
        env = {
            "SIGNOZ_URL": f"http://127.0.0.1:{self.server.server_port}",
            "SIGNOZ_API_KEY": "test-api-key",
        }
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False):
            for name in ("SIGNOZ_API_URL", "ORISO_ASSETS_DIR"):
                os.environ.pop(name, None)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = MODULE.main(
                    [
                        "--environment",
                        "pre-dev",
                        "--cluster-name",
                        "oriso-predev",
                        *argv,
                    ]
                )
        printed = out.getvalue() + err.getvalue()
        self.assertNotIn("test-api-key", printed)
        return code, printed

    def _counts(self) -> tuple[int, int, int, int]:
        return (
            len(self.state.dashboards),
            len(self.state.rules),
            self.state.channel_writes,
            len(self.state.test_rules),
        )

    def test_provision_is_idempotent_and_never_writes_channels(self) -> None:
        for _ in range(2):
            code, printed = self._main("--mode", "provision")
            self.assertEqual(code, 0)
            self.assertIn("telemetry not checked", printed)
        self.assertEqual(self._counts(), (3, 6, 0, 0))

        code, printed = self._main("--mode", "check")
        self.assertEqual(code, 0)
        self.assertIn("environment separation for pre-dev", printed)
        self.assertEqual(self._counts(), (3, 6, 0, 0))

    def test_full_check_rejects_an_empty_panel_and_unusable_points(self) -> None:
        self._main("--mode", "provision")
        before = self._counts()
        now = time.time_ns() // 1_000_000
        metric = "oriso.live_chat.queue.visibility"
        for point in (
            None,
            {"timestamp": now - 60_000, "value": 1, "partial": True},
            {"timestamp": now - 60_000, "value": None},
            {"timestamp": now - 60_000, "value": "NaN"},
            {"timestamp": now - 60_000, "value": True},
            {"timestamp": now - 16 * 60_000, "value": 1},
            {"timestamp": now + 60_000, "value": 1},
        ):
            with self.subTest(point=point):
                aggregations = [] if point is None else [{"index": 0, "series": [{"values": [point]}]}]
                self.state.query_results[metric] = [{"queryName": "A", "aggregations": aggregations}]
                with self.assertRaisesRegex(RuntimeError, "oriso-live-chat-pre-dev / queue.*no fresh numeric"):
                    self._main("--mode", "check")
        self.assertEqual(self._counts(), before)

    def test_full_check_requires_dimensions_on_each_contributing_series(self) -> None:
        self._main("--mode", "provision")
        fresh = time.time_ns() // 1_000_000 - 60_000
        demand = {"key": {"name": "demand"}, "value": "sensitive-fixture"}
        valid = {"key": {"name": "outcome"}, "value": "observed"}
        for invalid in ([], [{"key": {"name": "outcome"}, "value": ""}]):
            with self.subTest(invalid=invalid):
                series = [
                    {"labels": [demand, valid], "values": [{"timestamp": fresh, "value": 0}]},
                    {"labels": [demand, *invalid], "values": [{"timestamp": fresh, "value": 1}]},
                ]
                self.state.query_results["oriso.live_chat.queue.visibility"] = [{
                    "queryName": "A", "aggregations": [{"index": 0, "series": series}],
                }]
                with self.assertRaisesRegex(RuntimeError, "oriso-live-chat-pre-dev / queue.*outcome") as error:
                    self._main("--mode", "check")
                self.assertNotIn("sensitive-fixture", str(error.exception))

    def test_full_check_rejects_evidence_for_the_wrong_query_or_aggregation(self) -> None:
        self._main("--mode", "provision")
        point = {"timestamp": time.time_ns() // 1_000_000 - 60_000, "value": 1}
        for name, index in (("B", 0), ("A", 1), ("A", True)):
            with self.subTest(name=name, index=index):
                self.state.query_results["oriso.live_chat.queue.visibility"] = [{
                    "queryName": name,
                    "aggregations": [{"index": index, "series": [{"values": [point]}]}],
                }]
                with self.assertRaisesRegex(RuntimeError, "oriso-live-chat-pre-dev / queue.*unexpected"):
                    self._main("--mode", "check")

    def test_workstation_check_is_read_only(self) -> None:
        self._main("--mode", "provision")
        self.state.queries = 0
        before = json.dumps(
            [self.state.dashboards, self.state.rules, self.state.channels],
            sort_keys=True,
        )

        code, _ = self._main("--mode", "check", "--skip-telemetry")
        self.assertEqual(code, 0)
        self.assertEqual(self.state.queries, 0)
        after = json.dumps(
            [self.state.dashboards, self.state.rules, self.state.channels],
            sort_keys=True,
        )
        self.assertEqual(before, after)

    def test_provision_stops_before_writing_when_the_channel_is_missing(self) -> None:
        self.state.channels.clear()
        with self.assertRaisesRegex(RuntimeError, "created by hand"):
            self._main("--mode", "provision")
        self.assertEqual(self._counts(), (0, 0, 0, 0))

    def test_test_rule_without_series_reports_not_sent(self) -> None:
        self.state.test_rule_alert_count = 0
        code, printed = self._main("--mode", "test-rule", "--rule", "unencrypted-room")
        self.assertEqual(code, 2)
        self.assertIn("NOT SENT", printed)
        self.assertNotIn("PASS", printed)

    def test_test_rule_sends_one_notification_without_storing_a_rule(self) -> None:
        code, printed = self._main("--mode", "test-rule", "--rule", "unencrypted-room")
        self.assertEqual(code, 0)
        self.assertIn("alertCount=1", printed)
        self.assertEqual(self._counts(), (0, 0, 0, 1))
        payload = self.state.test_rules[0]
        self.assertTrue(payload["alert"].endswith(" [test]"))
        self.assertEqual(
            payload["condition"]["thresholds"]["spec"][0]["channels"],
            ["oriso-alerts-slack"],
        )
        self.assertNotIn("_orisoSlug", payload)

    def test_structural_proof_reads_back_without_querying_telemetry(self) -> None:
        """The default hook run must not depend on telemetry already flowing."""
        dashboards, alerts = MODULE.load_assets(ROOT / "files" / "signoz")
        dashboards, alerts = MODULE.materialize_assets(
            dashboards,
            alerts,
            environment="dev",
            cluster_name="oriso-dev",
            channel_name="oriso-alerts-slack",
        )
        client = MODULE.SignozClient(
            f"http://127.0.0.1:{self.server.server_port}", "test-api-key"
        )
        MODULE.upsert_dashboards(client, dashboards)
        MODULE.upsert_alerts(client, alerts)

        # The fake backend only has pre-dev telemetry, so a telemetry proof for
        # dev would fail; the structural proof must still pass.
        MODULE.prove_conformance(
            client,
            dashboards,
            alerts,
            environment="dev",
            cluster_name="oriso-dev",
            channel_name="oriso-alerts-slack",
            telemetry=False,
        )
        self.assertEqual(self.state.queries, 0)

        del self.state.dashboards[next(iter(self.state.dashboards))]
        with self.assertRaisesRegex(RuntimeError, "managed dashboard is missing"):
            MODULE.prove_conformance(
                client,
                dashboards,
                alerts,
                environment="dev",
                cluster_name="oriso-dev",
                channel_name="oriso-alerts-slack",
                telemetry=False,
            )

    def test_e2e_dashboards_are_provisioned_and_read_back_when_enabled(self) -> None:
        dashboards, alerts = MODULE.load_assets(
            ROOT / "files" / "signoz", include_e2e=True
        )
        dashboards, alerts = MODULE.materialize_assets(
            dashboards,
            alerts,
            environment="dev",
            cluster_name="oriso-dev",
            channel_name="oriso-alerts-slack",
        )
        self.assertEqual(len(dashboards), 5)
        client = MODULE.SignozClient(
            f"http://127.0.0.1:{self.server.server_port}", "test-api-key"
        )
        MODULE.upsert_dashboards(client, dashboards)
        MODULE.upsert_alerts(client, alerts)
        self.assertEqual(len(self.state.dashboards), 5)
        MODULE.prove_conformance(
            client,
            dashboards,
            alerts,
            environment="dev",
            cluster_name="oriso-dev",
            channel_name="oriso-alerts-slack",
            telemetry=False,
        )

    def test_full_check_accepts_quiet_e2e_queries_and_legacy_groups(self) -> None:
        self.assertEqual(self._main("--mode", "provision", "--e2e-dashboards")[0], 0)
        self.state.trace_responses["pass_rate_pct"] = {
            "type": "time_series",
            "data": {"results": [{"queryName": "A", "aggregations": [{
                "index": 1,
                "series": [{
                    "labels": [{"key": {"name": "test.id"}, "value": None}],
                    "values": [{"timestamp": 1_750_000_000_000, "value": 0, "partial": True}],
                }],
            }]}]},
        }
        code, printed = self._main("--mode", "check", "--e2e-dashboards")
        self.assertEqual(code, 0)
        self.assertIn("environment separation for pre-dev", printed)
        self.assertEqual(self._counts(), (5, 6, 0, 0))

        self.state.query_results["oriso.live_chat.queue.visibility"] = [{"queryName": "A", "aggregations": None}]
        with self.assertRaisesRegex(RuntimeError, "oriso-live-chat-pre-dev / queue.*no fresh numeric"):
            self._main("--mode", "check", "--e2e-dashboards")

    def test_full_check_rejects_malformed_optional_e2e_results(self) -> None:
        self.assertEqual(self._main("--mode", "provision", "--e2e-dashboards")[0], 0)
        bad_results = (
            {},
            {"queryName": "wrong", "aggregations": None},
            {"queryName": "A", "aggregations": {}},
            {"queryName": "A", "aggregations": [{"index": 0, "series": []}, {"index": 0, "series": []}]},
            {"queryName": "A", "aggregations": [{"index": 2, "series": []}]},
            {"queryName": "A", "aggregations": [{"index": 0, "series": {}}]},
            {"queryName": "A", "aggregations": [{"index": 0, "series": [{"values": "invalid"}]}]},
            {"queryName": "A", "aggregations": [{"index": 0, "series": [
                {"values": [{"timestamp": "invalid", "value": 0}]},
            ]}]},
        )
        for result in bad_results:
            with self.subTest(result=result):
                self.state.trace_responses["pass_rate_pct"] = {
                    "type": "time_series",
                    "data": {"results": [result]},
                }
                with self.assertRaisesRegex(RuntimeError, "oriso-e2e-test-health-pre-dev / pass-rate: unexpected"):
                    self._main("--mode", "check", "--e2e-dashboards")

    def test_e2e_dashboards_flag_reads_a_strict_true_from_the_environment(self) -> None:
        for value, expected in (
            ("true", True),
            ("TRUE", True),
            ("false", False),
            (None, False),
            ("1", False),
            ("yes", False),
        ):
            with self.subTest(value=value), mock.patch.dict(os.environ):
                os.environ.pop("ORISO_E2E_DASHBOARDS", None)
                if value is not None:
                    os.environ["ORISO_E2E_DASHBOARDS"] = value
                self.assertIs(MODULE._parse_args([]).e2e_dashboards, expected)


if __name__ == "__main__":
    unittest.main()
