#!/usr/bin/env python3
"""End-to-end HTTP contract for the SigNoz provision/check client."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import threading
import time
import unittest
from uuid import UUID
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
        self.platform_nodes: dict[str, int] = {"node-a": 1, "node-b": 0}
        self.covered_platform_nodes: dict[str, int] = {"node-a": 0, "node-b": 1}
        self.stale_platform_samples = False
        self.heartbeat_samples = True
        self.missing_platform_companion = False
        self.unreachable_datastore = False
        self.query_results: dict[str, list[dict[str, Any]]] = {}
        self.trace_responses: dict[str, dict[str, Any]] = {}
        self.browser_vitals_queries: list[dict[str, Any]] = []
        self.browser_vitals_response: dict[str, Any] = {}
        self.browser_vitals_status = 200
        self.redirect_browser_vitals = False
        self.redirect_dashboard_reads = False
        self.redirect_requests = 0


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

    def _store_dashboard(self, identifier: str, body: dict[str, Any], status: int) -> None:
        # SigNoz v0.135.1 serializes legacy metric tag keys as attributes.
        for query in MODULE.dashboard_builder_queries(body):
            for group in query.get("groupBy", []):
                if group.get("fieldContext") == "tag":
                    group["fieldContext"] = "attribute"
        self.state.dashboards[identifier] = {"id": identifier, **body}
        self._send(status, self.state.dashboards[identifier])

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/redirect-target":
            self.state.redirect_requests += 1
        if not self._require_key():
            return
        if self.path == "/api/v1/channels":
            self._send(200, list(self.state.channels.values()))
        elif self.path == "/api/v2/dashboards":
            if self.state.redirect_dashboard_reads:
                self.send_response(302)
                self.send_header("Location", "/redirect-target")
                self.end_headers()
                return
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
            identifier = str(UUID(int=len(self.state.dashboards) + 1))
            self._store_dashboard(identifier, body, 201)
        elif self.path == "/api/v2/rules":
            identifier = f"rule-{len(self.state.rules) + 1}"
            self.state.rules[identifier] = {"id": identifier, **body}
            self._send(201, self.state.rules[identifier])
        elif self.path == "/api/v5/query_range":
            self.state.queries += 1
            if body.get("requestType") == "raw":
                self.state.browser_vitals_queries.append(body)
                if self.state.redirect_browser_vitals:
                    self.send_response(302)
                    self.send_header("Location", "/redirect-target")
                    self.end_headers()
                else:
                    self._send(self.state.browser_vitals_status, self.state.browser_vitals_response)
                return
            entry = body["compositeQuery"]["queries"][0]
            query = entry["spec"]
            if entry["type"] == "promql":
                expression = query["query"]
                grouping = re.search(r'(?:min|max|sum) by \(([^)]+)\)', expression)[1]
                fields = json.loads(f"[{grouping}]")
                raw = expression.split(") (", 1)[1].startswith("present_over_time(")
                if expression.startswith("time() * "):
                    nodes = {"fixture": body["end"] // 1000} if self.state.heartbeat_samples else {}
                elif "k8s.node.condition" in expression:
                    nodes = self.state.platform_nodes if raw else self.state.covered_platform_nodes
                elif "k8s.volume." in expression:
                    nodes = {} if self.state.missing_platform_companion and not raw else {"fixture": 1 if raw else 3}
                elif "mysql.uptime" in expression:
                    outage_query = " unless " in expression
                    if outage_query:
                        nodes = {"fixture": int(self.state.unreachable_datastore)}
                    else:
                        nodes = {} if self.state.unreachable_datastore else {"fixture": 1}
                else:
                    nodes = {"fixture": 1 if raw else 0}
                self._send(200, {
                    "type": "time_series",
                    "data": {"results": [{"queryName": "A", "aggregations": [{
                        "index": 0,
                        "series": [{
                            "labels": [{
                                "key": {"name": key}, "value": (
                                    "oriso-dev" if key == "k8s.cluster.name" else
                                    "dev" if key == "deployment.environment" else name
                                ),
                            } for key in fields],
                            "values": [{"timestamp": body["end"] - (
                                60_000 if self.state.stale_platform_samples else 0
                            ), "value": value}],
                        } for name, value in nodes.items()],
                    }]}]},
                })
                return
            if query["signal"] in {"traces", "logs"}:
                alias = query["aggregations"][0].get("alias", "")
                response = self.state.trace_responses.get(alias) if query["signal"] == "traces" else None
                self._send(200, response or {
                    "type": "time_series",
                    "data": {"results": [{"queryName": query["name"], "aggregations": None}]},
                })
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
            self._store_dashboard(identifier, body, 200)
        elif self.path.startswith("/api/v2/rules/"):
            self.state.rules[identifier] = {"id": identifier, **body}
            self._send(204)
        else:
            self._send(404, {"error": "not found"})


class HttpFlowTest(unittest.TestCase):
    now_ms = 1_750_000_000_000

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
            for name in ("SIGNOZ_API_URL", "ORISO_ASSETS_DIR", "ORISO_AGENT_FORWARDING"):
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

    def test_agent_forwarding_preserves_slack_and_reconciles_existing_rule_ids(self) -> None:
        self.state.channels["agent"] = {"id": "agent", "name": "oriso-alerts-agents", "type": "webhook"}
        dev = ("--environment", "dev", "--cluster-name", "oriso-dev", "--deployment-identity")
        self.assertEqual(self._main(*dev, "--mode", "provision", "--agent-forwarding")[0], 0)
        original_ids = set(self.state.rules)
        forwarded = set()
        for alert in self.state.rules.values():
            rule = alert["labels"].get("oriso_forward_rule")
            routes = alert["condition"]["thresholds"]["spec"][0]["channels"]
            self.assertEqual(routes[0], "oriso-alerts-slack")
            if rule:
                forwarded.add(rule)
                self.assertEqual(routes, ["oriso-alerts-slack", "oriso-alerts-agents"])
                self.assertEqual(alert["annotations"]["oriso_forward_value"], "{{$value}}")
                self.assertEqual(alert["condition"]["compositeQuery"].get("unit"), "")
            else:
                self.assertEqual(routes, ["oriso-alerts-slack"])
        self.assertEqual(forwarded, MODULE.REQUIRED_ALERT_SLUGS)
        self.assertEqual(self._counts(), (3, 7, 0, 0))

        first = next(alert for alert in self.state.rules.values() if alert["labels"].get("oriso_forward_rule"))
        first["condition"]["thresholds"]["spec"][0]["channels"].append("unexpected-channel")
        with self.assertRaisesRegex(RuntimeError, "stored alert route drift"):
            self._main(*dev, "--mode", "check", "--skip-telemetry", "--agent-forwarding")

        del self.state.channels["agent"]
        self.assertEqual(self._main(*dev, "--mode", "provision")[0], 0)
        self.assertEqual(set(self.state.rules), original_ids)
        for alert in self.state.rules.values():
            self.assertNotIn("oriso_forward_rule", alert["labels"])
            self.assertNotIn("oriso_forward_value", alert["annotations"])
            self.assertEqual(alert["condition"]["thresholds"]["spec"][0]["channels"], ["oriso-alerts-slack"])

    def test_agent_forwarding_requires_the_webhook_before_writes(self) -> None:
        dev = ("--environment", "dev", "--cluster-name", "oriso-dev")
        for type_name in (None, "slack"):
            with self.subTest(type_name=type_name):
                if type_name:
                    self.state.channels["agent"] = {"name": "oriso-alerts-agents", "type": type_name}
                with self.assertRaisesRegex(RuntimeError, "oriso-alerts-agents.*webhook"):
                    self._main(*dev, "--mode", "provision", "--agent-forwarding")
                self.assertEqual(self._counts(), (0, 0, 0, 0))

    def test_agent_forwarding_rejects_non_dev_before_writes(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "agent forwarding.*Dev"):
            self._main("--mode", "provision", "--agent-forwarding")
        self.assertEqual(self._counts(), (0, 0, 0, 0))

    def test_agent_forwarding_rule_test_reports_evaluation_and_preserves_both_routes(self) -> None:
        self.state.channels["agent"] = {"name": "oriso-alerts-agents", "type": "webhook"}
        code, printed = self._main(
            "--environment", "dev", "--cluster-name", "oriso-dev",
            "--mode", "test-rule", "--rule", "unencrypted-room", "--agent-forwarding",
        )
        self.assertEqual(code, 0)
        self.assertIn("Verify both receiver receipts", printed)
        self.assertEqual(self._counts(), (0, 0, 0, 1))
        payload = self.state.test_rules[0]
        self.assertEqual(
            payload["condition"]["thresholds"]["spec"][0]["channels"],
            ["oriso-alerts-slack", "oriso-alerts-agents"],
        )
        self.assertEqual(payload["labels"]["oriso_forward_rule"], "unencrypted-room")

    def test_workstation_check_rejects_redirect_before_following_it(self) -> None:
        self.state.redirect_dashboard_reads = True
        with self.assertRaisesRegex(MODULE.SignozHttpError, "302: response body omitted"):
            self._main("--mode", "check", "--skip-telemetry")
        self.assertEqual(self.state.redirect_requests, 0)

    def test_fake_backend_normalizes_legacy_tag_group_by_to_attribute(self) -> None:
        # Guards the fake against drifting from SigNoz v0.135.1, which stores a
        # legacy `tag` group-by field as an `attribute`.
        import urllib.request

        group = {"name": "k8s.pod.name", "fieldContext": "tag"}
        dashboard = {
            "title": "tag normalization",
            "widgets": [
                {"kind": "signoz/BuilderQuery", "spec": {"groupBy": [group]}}
            ],
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/api/v2/dashboards",
            data=json.dumps(dashboard).encode(),
            headers={
                "SIGNOZ-API-KEY": "test-api-key",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            returned = json.load(response)
        for stored in (returned, next(iter(self.state.dashboards.values()))):
            queries = MODULE.dashboard_builder_queries(stored)
            self.assertEqual(
                [g["fieldContext"] for q in queries for g in q["groupBy"]],
                ["attribute"],
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

    def _browser_vitals_response(self) -> dict[str, Any]:
        rows = [
            {"data": {
                "service_name": service,
                "metric_name": metric,
                "samples": 1,
                "latest_ms": self.now_ms - 1,
            }}
            for service in ("frontend", "admin")
            for metric in ("lcp.count", "cls", "inp.count", "ttfb.count", "fcp.count")
        ]
        return {"type": "raw", "data": {"results": [{
            "queryName": "BrowserVitals", "rows": rows, "nextCursor": "",
        }]}}

    def _browser_vitals(self, *argv: str) -> tuple[int, str]:
        with mock.patch.object(MODULE.time, "time", return_value=self.now_ms / 1000):
            return self._main(
                "--mode", "check-browser-vitals", "--environment", "dev",
                "--cluster-name", "", "--assets-dir", "/missing-assets", *argv,
            )

    def test_browser_vitals_checks_both_apps_after_the_requested_start(self) -> None:
        self.state.browser_vitals_response = self._browser_vitals_response()
        self.state.channels.clear()
        since = self.now_ms - 120_000

        code, printed = self._browser_vitals("--since-unix-ms", str(since))

        self.assertEqual(code, 0)
        report = json.loads(printed)
        self.assertEqual(report["missing"], [])
        self.assertEqual(report["services"]["admin"]["cls"]["samples"], 1)
        request, = self.state.browser_vitals_queries
        self.assertEqual((request["start"], request["end"]), (since, self.now_ms))
        query, = request["compositeQuery"]["queries"]
        self.assertEqual(query["type"], "clickhouse_sql")
        sql = query["spec"]["query"]
        self.assertIn("resource_attrs['deployment.environment'] = 'dev'", sql)
        self.assertIn("resource_attrs['oriso.signal.source'] = 'browser'", sql)
        self.assertEqual(self._counts(), (0, 0, 0, 0))

    def test_browser_vitals_fails_when_only_one_app_has_a_required_metric(self) -> None:
        response = self._browser_vitals_response()
        rows = response["data"]["results"][0]["rows"]
        rows[:] = [row for row in rows if (
            row["data"]["service_name"], row["data"]["metric_name"]
        ) != ("admin", "inp.count")]
        self.state.browser_vitals_response = response

        code, printed = self._browser_vitals()

        self.assertEqual(code, 1)
        report = json.loads(printed)
        self.assertEqual(report["missing"], ["admin/inp"])
        self.assertEqual(report["startUnixMs"], self.now_ms - 30 * 60_000)

    def test_browser_vitals_rejects_partial_duplicate_and_stale_evidence(self) -> None:
        for defect in ("warning", "duplicate", "stale", "foreign"):
            with self.subTest(defect=defect):
                response = self._browser_vitals_response()
                rows = response["data"]["results"][0]["rows"]
                if defect == "warning":
                    response["warning"] = ["partial result"]
                elif defect == "duplicate":
                    rows.append(rows[0])
                elif defect == "stale":
                    rows[0]["data"]["latest_ms"] = self.now_ms - 30 * 60_000 - 1
                else:
                    rows[0]["data"]["service_name"] = "foreign-service"
                self.state.browser_vitals_response = response
                with self.assertRaisesRegex(RuntimeError, "incomplete browser vitals evidence"):
                    self._browser_vitals()

    def test_browser_vitals_does_not_forward_the_api_key_on_redirect(self) -> None:
        self.state.redirect_browser_vitals = True

        with self.assertRaisesRegex(RuntimeError, "returned 302"):
            self._browser_vitals()

        self.assertEqual(self.state.redirect_requests, 0)

    def test_browser_vitals_reports_http_status_without_logging_response_detail(self) -> None:
        self.state.browser_vitals_status = 403
        self.state.browser_vitals_response = {"error": "DO-NOT-PRINT test-api-key"}

        with self.assertRaisesRegex(RuntimeError, "returned 403") as failure:
            self._browser_vitals()

        self.assertNotIn("DO-NOT-PRINT", str(failure.exception))
        self.assertNotIn("test-api-key", str(failure.exception))

    def test_browser_vitals_refuses_a_future_window_before_querying(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "timestamp before now"):
            self._browser_vitals("--since-unix-ms", str(self.now_ms + 1))

        self.assertEqual(self.state.browser_vitals_queries, [])

    def test_provision_stops_before_writing_when_the_channel_is_missing(self) -> None:
        self.state.channels.clear()
        with self.assertRaisesRegex(RuntimeError, "created by hand"):
            self._main("--mode", "provision")
        self.assertEqual(self._counts(), (0, 0, 0, 0))

    def test_platform_rule_requires_current_coverage_before_writing_assets(self) -> None:
        args = ("--environment", "dev", "--cluster-name", "oriso-dev")
        self.state.covered_platform_nodes = {"node-a": 0}
        with self.assertRaisesRegex(RuntimeError, "complete sampled data"):
            self._main("--mode", "provision", "--platform-alerts", *args)
        self.assertEqual(self._counts(), (0, 0, 0, 0))

        self.state.covered_platform_nodes = {"node-a": 0, "node-b": 1}
        self.state.stale_platform_samples = True
        with self.assertRaisesRegex(RuntimeError, "complete sampled data"):
            self._main("--mode", "provision", "--platform-alerts", *args)
        self.assertEqual(self._counts(), (0, 0, 0, 0))

        self.state.stale_platform_samples = False
        self.state.missing_platform_companion = True
        with self.assertRaisesRegex(RuntimeError, "complete sampled data for platform-pvc-capacity"):
            self._main("--mode", "provision", "--platform-alerts", *args)
        self.assertEqual(self._counts(), (0, 0, 0, 0))

    def test_heartbeat_preflights_channel_and_signal_and_disables_the_same_rule(self) -> None:
        dev = ("--environment", "dev", "--cluster-name", "oriso-dev")
        provision = ("--mode", "provision", *dev)
        check = ("--mode", "check", "--skip-telemetry", *dev)
        channel = {
            "name": "oriso-dev-heartbeat", "type": "webhook",
            "data": json.dumps({"webhook_configs": [{
                "url": "https://watchdog.example.invalid/pulse",
                "send_resolved": False,
                "http_config": {"basic_auth": {"username": "fixture", "password": "fixture"}},
            }]}),
        }
        with self.assertRaisesRegex(RuntimeError, "dedicated HTTPS webhook"):
            self._main(*provision, "--heartbeat")
        self.assertEqual(self._counts(), (0, 0, 0, 0))
        self.state.channels["heartbeat"] = channel
        self.state.heartbeat_samples = False
        with self.assertRaisesRegex(RuntimeError, "requires a current sample"):
            self._main(*provision, "--heartbeat")
        self.assertEqual(self._counts(), (0, 0, 0, 0))

        self._main(*provision)
        self.assertEqual(self._counts(), (3, 6, 0, 0))
        self.state.heartbeat_samples = True
        self._main(*provision, "--heartbeat")
        rule_id, heartbeat = next(
            (rid, item) for rid, item in self.state.rules.items() if item["alert"] == "ORISO dev | SigNoz heartbeat"
        )
        self.assertFalse(heartbeat["disabled"])
        self.assertEqual(heartbeat["annotations"]["oriso_evaluated_at"], "{{$value}}")
        self.assertEqual(self._main(*check, "--heartbeat")[0], 0)
        heartbeat["annotations"]["oriso_evaluated_at"] = "{{ $value }}"
        with self.assertRaisesRegex(RuntimeError, "stored SigNoz heartbeat settings drift"):
            self._main(*check, "--heartbeat")

        del self.state.channels["heartbeat"]
        self.state.heartbeat_samples = False
        queries_before = self.state.queries
        self._main(*provision)
        self.assertTrue(self.state.rules[rule_id]["disabled"])
        self.assertEqual(self.state.queries, queries_before)
        self.assertEqual(self._counts(), (3, 7, 0, 0))
        self.assertEqual(self._main(*check)[0], 0)

    def test_heartbeat_never_uses_the_synthetic_test_rule_api(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unknown catalog alert"):
            self._main(
                "--mode", "test-rule", "--environment", "dev", "--cluster-name", "oriso-dev",
                "--heartbeat", "--rule", "signoz-heartbeat",
            )
        self.assertEqual(self._counts(), (0, 0, 0, 0))

    def test_platform_rule_reconciles_the_same_id_after_opt_out(self) -> None:
        config = ("--mode", "provision", "--environment", "dev", "--cluster-name", "oriso-dev")
        for _ in range(2):
            self._main(*config, "--platform-alerts")
        rule_id, saved = next(
            (rid, item) for rid, item in self.state.rules.items()
            if item["alert"] == "ORISO dev | Kubernetes node is not ready"
        )
        self.assertEqual(self._counts(), (3, 6 + len(MODULE.PLATFORM_SIGNALS), 0, 0))
        self.assertFalse(saved["disabled"])

        self.state.covered_platform_nodes = {}
        queries_before = self.state.queries
        self._main(*config)
        self.assertEqual(self._counts(), (3, 6 + len(MODULE.PLATFORM_SIGNALS), 0, 0))
        self.assertTrue(self.state.rules[rule_id]["disabled"])
        disabled = [item for item in self.state.rules.values() if item["disabled"]]
        self.assertEqual(len(disabled), len(MODULE.PLATFORM_SIGNALS))
        self.assertEqual(self.state.queries, queries_before)

        self.state.covered_platform_nodes = {"node-a": 0, "node-b": 1}
        self._main(*config, "--platform-alerts")
        self.assertFalse(self.state.rules[rule_id]["disabled"])
        self.assertEqual(self._counts(), (3, 6 + len(MODULE.PLATFORM_SIGNALS), 0, 0))

    def test_platform_activation_rejects_datastore_outage_before_writes(self) -> None:
        self.state.unreachable_datastore = True
        args = ("--mode", "provision", "--environment", "dev", "--cluster-name", "oriso-dev")
        with self.assertRaisesRegex(RuntimeError, "complete sampled data for platform-mariadb"):
            self._main(*args, "--platform-alerts")
        self.assertEqual(self._counts(), (0, 0, 0, 0))

    def test_platform_rule_rejects_ambiguous_ids(self) -> None:
        args = ("--mode", "provision", "--environment", "dev", "--cluster-name", "oriso-dev")
        for rid in ("manual-1", "manual-2"):
            self.state.rules[rid] = {"id": rid, "alert": "ORISO dev | Kubernetes node is not ready"}
        for flag in ((), ("--platform-alerts",)):
            with self.assertRaisesRegex(RuntimeError, "ambiguous ownership"):
                self._main(*args, *flag)
            self.assertEqual(self._counts(), (0, 2, 0, 0))

    def test_platform_rule_detects_unmanaged_name_before_writes(self) -> None:
        args = ("--mode", "provision", "--environment", "dev", "--cluster-name", "oriso-dev")
        existing = {"id": "manual", "alert": "ORISO dev | Kubernetes node is not ready", "disabled": False}
        self.state.rules["manual"] = existing.copy()
        with self.assertRaisesRegex(RuntimeError, "platform alert name is already in use"):
            self._main(*args, "--platform-alerts")
        self.assertEqual(self._counts(), (0, 1, 0, 0))
        self._main(*args)
        self.assertEqual(self.state.rules["manual"], existing)

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
