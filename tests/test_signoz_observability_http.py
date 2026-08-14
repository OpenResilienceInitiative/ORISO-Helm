#!/usr/bin/env python3
"""End-to-end HTTP contract for the SigNoz provision/check client."""

from __future__ import annotations

import importlib.util
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "signoz_observability", ROOT / "scripts" / "signoz_observability.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class State:
    def __init__(self) -> None:
        self.channels: dict[str, dict[str, Any]] = {}
        self.dashboards: dict[str, dict[str, Any]] = {}
        self.rules: dict[str, dict[str, Any]] = {}
        self.route_tests = 0


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
        elif self.path == "/api/v1/route_policies":
            routes = [
                {
                    "id": f"route-{rule_id}",
                    "kind": "rule",
                    "channels": rule["condition"]["thresholds"]["spec"][0]["channels"],
                }
                for rule_id, rule in self.state.rules.items()
            ]
            self._send(200, routes)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._require_key():
            return
        body = self._body()
        if self.path == "/api/v1/channels":
            identifier = "channel-1"
            self.state.channels[identifier] = {
                "id": identifier,
                "name": body["name"],
                "type": "slack",
            }
            self._send(201, self.state.channels[identifier])
        elif self.path == "/api/v1/channels/test":
            self.state.route_tests += 1
            self._send(204)
        elif self.path == "/api/v2/dashboards":
            identifier = f"dashboard-{len(self.state.dashboards) + 1}"
            self.state.dashboards[identifier] = {"id": identifier, **body}
            self._send(201, self.state.dashboards[identifier])
        elif self.path == "/api/v2/rules":
            identifier = f"rule-{len(self.state.rules) + 1}"
            self.state.rules[identifier] = {"id": identifier, **body}
            self._send(201, self.state.rules[identifier])
        elif self.path == "/api/v5/query_range":
            expression = body["compositeQuery"]["queries"][0]["spec"]["filter"][
                "expression"
            ]
            results: list[dict[str, Any]] = []
            if "deployment.environment = 'pre-dev'" in expression:
                results = [
                    {
                        "aggregations": [
                            {"series": [{"values": [[1_750_000_000_000, 1.0]]}]}
                        ]
                    }
                ]
            self._send(200, {"data": {"results": results}})
        else:
            self._send(404, {"error": "not found"})

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._require_key():
            return
        body = self._body()
        identifier = self.path.rsplit("/", 1)[1]
        if self.path.startswith("/api/v1/channels/"):
            self.state.channels[identifier] = {
                "id": identifier,
                "name": body["name"],
                "type": "slack",
            }
            self._send(204)
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

    def test_provision_is_idempotent_and_conformance_is_read_only(self) -> None:
        dashboards, alerts = MODULE.load_assets(ROOT / "files" / "signoz")
        dashboards, alerts = MODULE.materialize_assets(
            dashboards,
            alerts,
            environment="pre-dev",
            cluster_name="oriso-predev",
            channel_name="ORISO Platform Alerts",
        )
        client = MODULE.SignozClient(
            f"http://127.0.0.1:{self.server.server_port}", "test-api-key"
        )
        receiver = MODULE.build_slack_receiver(
            channel_name="ORISO Platform Alerts",
            webhook_url="https://hooks.slack.test/redacted",
            environment="pre-dev",
            cluster_name="oriso-predev",
        )

        client.wait_ready(timeout_seconds=1)
        for _ in range(2):
            MODULE.upsert_channel(client, receiver)
            MODULE.upsert_dashboards(client, dashboards)
            MODULE.upsert_alerts(client, alerts)
        client.request("POST", "/api/v1/channels/test", receiver, expected=(204,))

        self.assertEqual(len(self.state.channels), 1)
        self.assertEqual(len(self.state.dashboards), 3)
        self.assertEqual(len(self.state.rules), 6)
        self.assertEqual(self.state.route_tests, 1)

        before = (
            len(self.state.channels),
            len(self.state.dashboards),
            len(self.state.rules),
            self.state.route_tests,
        )
        MODULE.prove_conformance(
            client,
            dashboards,
            alerts,
            environment="pre-dev",
            cluster_name="oriso-predev",
            channel_name="ORISO Platform Alerts",
        )
        self.assertEqual(
            before,
            (
                len(self.state.channels),
                len(self.state.dashboards),
                len(self.state.rules),
                self.state.route_tests,
            ),
        )


if __name__ == "__main__":
    unittest.main()
