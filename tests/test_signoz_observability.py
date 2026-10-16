#!/usr/bin/env python3
"""Contracts for ORISO-managed SigNoz dashboards, alerts, and conformance."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "signoz_observability.py"
ASSETS = ROOT / "files" / "signoz"

SPEC = importlib.util.spec_from_file_location("signoz_observability", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AssetContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dashboards, self.alerts = MODULE.load_assets(ASSETS)
        self.dashboards, self.alerts = MODULE.materialize_assets(
            self.dashboards,
            self.alerts,
            environment="pre-dev",
            cluster_name="oriso-predev",
            channel_name="oriso-alerts-slack",
        )

    def test_assets_cover_diagnosis_and_platform_failure_modes(self) -> None:
        self.assertEqual(
            {item["name"] for item in self.dashboards},
            {
                "oriso-live-chat-pre-dev",
                "oriso-service-reliability-pre-dev",
                "oriso-platform-health-pre-dev",
            },
        )
        self.assertEqual(len(self.alerts), 6)
        encoded = json.dumps(
            {"dashboards": self.dashboards, "alerts": self.alerts},
            sort_keys=True,
        )
        for metric in (
            "oriso.live_chat.queue.visibility",
            "oriso.live_chat.routing.decisions",
            "oriso.live_chat.availability.store.operations",
            "oriso.matrix.room.creation",
            "oriso.matrix.event.processing",
            "oriso.matrix.side_effect.operations",
            "userservice.provisioning.compensation.attempts",
            "userservice.outbound.http.calls",
            "userservice.replica.constraint.violated",
            "k8s.volume.available",
            "otelcol_receiver_accepted_metric_points",
        ):
            self.assertIn(metric, encoded)

        MODULE.validate_asset_contract(
            self.dashboards,
            self.alerts,
            environment="pre-dev",
            cluster_name="oriso-predev",
            channel_name="oriso-alerts-slack",
        )

    def test_assets_are_native_current_signoz_schemas_and_environment_scoped(
        self,
    ) -> None:
        for dashboard in self.dashboards:
            self.assertEqual(dashboard["schemaVersion"], "v6")
            self.assertEqual(dashboard["generateName"], False)
            self.assertEqual(dashboard["spec"]["variables"], [])
            for query in MODULE.dashboard_builder_queries(dashboard):
                self.assertIn(
                    "deployment.environment = 'pre-dev'", query["filter"]["expression"]
                )

        for alert in self.alerts:
            self.assertEqual(alert["schemaVersion"], "v2alpha1")
            self.assertEqual(alert["version"], "v5")
            self.assertFalse(alert["disabled"])
            self.assertEqual(alert["notificationSettings"]["groupBy"], ["environment"])
            threshold = alert["condition"]["thresholds"]["spec"][0]
            self.assertEqual(threshold["channels"], ["oriso-alerts-slack"])
            query = alert["condition"]["compositeQuery"]["queries"][0]["spec"]
            self.assertIn(
                "deployment.environment = 'pre-dev'", query["filter"]["expression"]
            )

    def test_assets_never_expose_protected_identifiers_or_content(self) -> None:
        encoded = json.dumps(
            {"dashboards": self.dashboards, "alerts": self.alerts},
            sort_keys=True,
        ).lower()
        for forbidden in (
            "user.id",
            "user_id",
            "room.id",
            "room_id",
            "message.body",
            "email",
            "username",
            "access_token",
            "password",
        ):
            self.assertNotIn(forbidden, encoded)


class ApiPayloadContractTest(unittest.TestCase):
    def setUp(self) -> None:
        dashboards, alerts = MODULE.load_assets(ASSETS)
        _, self.alerts = MODULE.materialize_assets(
            dashboards,
            alerts,
            environment="pre-dev",
            cluster_name="oriso-predev",
            channel_name="oriso-alerts-slack",
        )

    def test_query_range_payload_executes_current_builder_query(self) -> None:
        payload = MODULE.build_query_range_payload(
            {
                "name": "A",
                "signal": "metrics",
                "aggregations": [
                    {
                        "metricName": "oriso.live_chat.routing.decisions",
                        "timeAggregation": "rate",
                        "spaceAggregation": "sum",
                    }
                ],
                "filter": {"expression": "deployment.environment = 'pre-dev'"},
                "groupBy": [],
            },
            start_ms=1_000,
            end_ms=61_000,
        )

        self.assertEqual(payload["schemaVersion"], "v1")
        self.assertEqual(payload["requestType"], "time_series")
        self.assertEqual(
            payload["compositeQuery"]["queries"][0]["type"], "builder_query"
        )
        self.assertTrue(payload["noCache"])

    def test_signal_data_detection_distinguishes_empty_from_fresh_results(self) -> None:
        self.assertFalse(
            MODULE.response_has_signal_data(
                {"status": "success", "data": {"data": {"results": []}}}
            )
        )
        self.assertTrue(
            MODULE.response_has_signal_data(
                {
                    "status": "success",
                    "data": {
                        "data": {
                            "results": [
                                {"aggregations": [{"series": [{"values": [[1, 1.0]]}]}]}
                            ]
                        }
                    },
                }
            )
        )
        self.assertFalse(
            MODULE.response_has_positive_signal_data(
                {
                    "status": "success",
                    "data": {
                        "data": {
                            "results": [
                                {"aggregations": [{"series": [{"values": [[1, 0.0]]}]}]}
                            ]
                        }
                    },
                }
            )
        )
        self.assertTrue(
            MODULE.response_has_positive_signal_data(
                {
                    "status": "success",
                    "data": {
                        "data": {
                            "results": [
                                {
                                    "aggregations": [
                                        {
                                            "series": [
                                                {
                                                    "values": [
                                                        {
                                                            "timestamp": 1,
                                                            "value": "2.5",
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    },
                }
            )
        )

    def test_alerts_reference_the_existing_channel_by_name(self) -> None:
        for alert in self.alerts:
            self.assertEqual(alert["preferredChannels"], ["oriso-alerts-slack"])
            self.assertEqual(
                alert["condition"]["thresholds"]["spec"][0]["channels"],
                ["oriso-alerts-slack"],
            )

    def test_test_rule_payload_never_reuses_the_managed_rule_name(self) -> None:
        payload = MODULE.build_test_rule(self.alerts, "unencrypted-room")
        managed = next(
            item for item in self.alerts if item["_orisoSlug"] == "unencrypted-room"
        )
        self.assertEqual(payload["alert"], managed["alert"] + " [test]")
        self.assertNotIn("_orisoSlug", payload)
        self.assertIn("_orisoSlug", managed, "the catalog copy must stay intact")
        with self.assertRaisesRegex(RuntimeError, "unknown catalog alert"):
            MODULE.build_test_rule(self.alerts, "no-such-alert")

    def test_environment_separation_query_does_not_corrupt_dev_cluster_name(
        self,
    ) -> None:
        query = {
            "filter": {
                "expression": (
                    "deployment.environment = 'dev' AND "
                    "k8s.cluster.name = 'oriso-dev'"
                )
            }
        }

        opposite = MODULE.opposite_environment_query(
            query, environment="dev", cluster_name="oriso-dev"
        )

        self.assertEqual(
            opposite["filter"]["expression"],
            "deployment.environment = 'pre-dev' AND "
            "k8s.cluster.name = 'oriso-predev'",
        )

    def test_api_error_redaction_never_prints_notification_credentials(self) -> None:
        detail = MODULE.redact_sensitive(
            '{"api_url":"https://hooks.slack.'
            + 'com/services/T/B/secret","token":"top-secret"}'
        )

        self.assertNotIn("hooks.slack.com", detail)
        self.assertNotIn("top-secret", detail)
        self.assertIn("[REDACTED]", detail)


class UpsertContractTest(unittest.TestCase):
    class Client:
        def __init__(self, responses: dict[tuple[str, str], object]) -> None:
            self.responses = responses
            self.calls: list[tuple[str, str, object, tuple[int, ...]]] = []

        def request(
            self,
            method: str,
            path: str,
            payload: object = None,
            expected: tuple[int, ...] = (200,),
        ) -> object:
            self.calls.append((method, path, payload, expected))
            return self.responses.get((method, path))

    def setUp(self) -> None:
        dashboards, alerts = MODULE.load_assets(ASSETS)
        self.dashboards, self.alerts = MODULE.materialize_assets(
            dashboards,
            alerts,
            environment="pre-dev",
            cluster_name="oriso-predev",
            channel_name="oriso-alerts-slack",
        )

    def test_empty_install_creates_dashboards_and_rules_but_no_channel(
        self,
    ) -> None:
        client = self.Client(
            {
                ("GET", "/api/v1/channels"): {
                    "status": "success",
                    "data": [
                        {"id": "c", "name": "oriso-alerts-slack", "type": "slack"}
                    ],
                },
                ("GET", "/api/v2/dashboards"): {
                    "status": "success",
                    "data": {"dashboards": []},
                },
                ("GET", "/api/v2/rules"): {"status": "success", "data": []},
            }
        )

        MODULE.require_channel(client, "oriso-alerts-slack")
        MODULE.upsert_dashboards(client, self.dashboards)
        MODULE.upsert_alerts(client, self.alerts)

        created_paths = [
            path for method, path, _, _ in client.calls if method == "POST"
        ]
        self.assertNotIn("/api/v1/channels", created_paths)
        self.assertFalse(
            [
                path
                for method, path, _, _ in client.calls
                if "channels" in path and method != "GET"
            ]
        )
        self.assertEqual(created_paths.count("/api/v2/dashboards"), 3)
        self.assertEqual(created_paths.count("/api/v2/rules"), 6)
        rule_payloads = [
            payload
            for method, path, payload, _ in client.calls
            if method == "POST" and path == "/api/v2/rules"
        ]
        self.assertTrue(all("_orisoSlug" not in payload for payload in rule_payloads))

    def test_missing_channel_is_reported_not_created(self) -> None:
        client = self.Client(
            {("GET", "/api/v1/channels"): {"status": "success", "data": []}}
        )
        with self.assertRaisesRegex(RuntimeError, "created by hand"):
            MODULE.require_channel(client, "oriso-alerts-slack")
        self.assertEqual(
            [(m, p) for m, p, _, _ in client.calls], [("GET", "/api/v1/channels")]
        )

    def test_existing_assets_are_updated_by_stable_identity(self) -> None:
        dashboard = self.dashboards[0]
        alert = self.alerts[0]
        client = self.Client(
            {
                ("GET", "/api/v2/dashboards"): {
                    "status": "success",
                    "data": {
                        "dashboards": [
                            {"id": "dashboard-id", "name": dashboard["name"]}
                        ]
                    },
                },
                ("GET", "/api/v2/rules"): {
                    "status": "success",
                    "data": [{"id": "rule-id", "alert": alert["alert"]}],
                },
            }
        )

        MODULE.upsert_dashboards(client, [dashboard])
        MODULE.upsert_alerts(client, [alert])

        self.assertIn(
            ("PUT", "/api/v2/dashboards/dashboard-id"),
            [(method, path) for method, path, _, _ in client.calls],
        )
        dashboard_payload = next(
            payload
            for method, path, payload, _ in client.calls
            if method == "PUT" and path.endswith("dashboard-id")
        )
        self.assertNotIn("generateName", dashboard_payload)
        self.assertIn(
            ("PUT", "/api/v2/rules/rule-id"),
            [(method, path) for method, path, _, _ in client.calls],
        )


class LiveConformanceContractTest(unittest.TestCase):
    def setUp(self) -> None:
        dashboards, alerts = MODULE.load_assets(ASSETS)
        self.dashboards, self.alerts = MODULE.materialize_assets(
            dashboards,
            alerts,
            environment="pre-dev",
            cluster_name="oriso-predev",
            channel_name="oriso-alerts-slack",
        )

    def test_live_dashboard_contract_rejects_a_removed_environment_filter(self) -> None:
        expected = self.dashboards[0]
        current = json.loads(json.dumps(expected))
        query = MODULE.dashboard_builder_queries(current)[0]
        query["filter"]["expression"] = "outcome = 'failure'"

        with self.assertRaisesRegex(RuntimeError, "stored dashboard query drift"):
            MODULE.validate_live_dashboard(current, expected, environment="pre-dev")

    def test_live_dashboard_contract_rejects_a_disabled_query(self) -> None:
        expected = self.dashboards[0]
        current = json.loads(json.dumps(expected))
        MODULE.dashboard_builder_queries(current)[0]["disabled"] = True

        with self.assertRaisesRegex(RuntimeError, "stored dashboard query drift"):
            MODULE.validate_live_dashboard(current, expected, environment="pre-dev")

    def test_live_alert_contract_rejects_a_different_notification_channel(self) -> None:
        expected = self.alerts[0]
        current = json.loads(json.dumps(expected))
        current["condition"]["thresholds"]["spec"][0]["channels"] = ["other"]

        with self.assertRaisesRegex(RuntimeError, "stored alert route drift"):
            MODULE.validate_live_alert(
                current,
                expected,
                environment="pre-dev",
                channel_name="oriso-alerts-slack",
            )

    def test_live_alert_contract_rejects_a_disabled_query(self) -> None:
        expected = self.alerts[0]
        current = json.loads(json.dumps(expected))
        current["condition"]["compositeQuery"]["queries"][0]["spec"]["disabled"] = True

        with self.assertRaisesRegex(RuntimeError, "stored alert query drift"):
            MODULE.validate_live_alert(
                current,
                expected,
                environment="pre-dev",
                channel_name="oriso-alerts-slack",
            )

    def test_live_dashboard_contract_rejects_queries_swapped_between_panels(
        self,
    ) -> None:
        """Two panels showing each other's data must not compare equal."""
        expected = next(
            dashboard
            for dashboard in self.dashboards
            if len(MODULE.dashboard_panel_queries(dashboard)) >= 2
        )
        current = json.loads(json.dumps(expected))
        panels = current["spec"]["panels"]
        first, second = list(panels)[:2]
        first_queries = panels[first]["spec"]["queries"]
        panels[first]["spec"]["queries"] = panels[second]["spec"]["queries"]
        panels[second]["spec"]["queries"] = first_queries

        with self.assertRaisesRegex(RuntimeError, "stored dashboard query drift"):
            MODULE.validate_live_dashboard(current, expected, environment="pre-dev")

    def test_live_dashboard_contract_rejects_a_renamed_panel(self) -> None:
        expected = self.dashboards[0]
        current = json.loads(json.dumps(expected))
        panel_id = next(iter(current["spec"]["panels"]))
        current["spec"]["panels"][panel_id]["spec"]["display"][
            "name"
        ] = "Something else"

        with self.assertRaisesRegex(RuntimeError, "panel title drift"):
            MODULE.validate_live_dashboard(current, expected, environment="pre-dev")

    def test_live_dashboard_contract_rejects_an_added_having_clause(self) -> None:
        """A restrictive HAVING blanks a panel while the query still succeeds."""
        expected = self.dashboards[0]
        current = json.loads(json.dumps(expected))
        MODULE.dashboard_builder_queries(current)[0]["having"] = {
            "expression": "A > 1000000"
        }

        with self.assertRaisesRegex(RuntimeError, "stored dashboard query drift"):
            MODULE.validate_live_dashboard(current, expected, environment="pre-dev")

    def test_live_dashboard_contract_rejects_a_panel_removed_from_the_grid(
        self,
    ) -> None:
        """A panel left in spec.panels but dropped from the layout is invisible."""
        expected = self.dashboards[0]
        current = json.loads(json.dumps(expected))
        current["spec"]["layouts"][0]["spec"]["items"].pop()

        with self.assertRaisesRegex(RuntimeError, "stored dashboard layout drift"):
            MODULE.validate_live_dashboard(current, expected, environment="pre-dev")

    def test_live_dashboard_contract_rejects_a_changed_group_by_context(
        self,
    ) -> None:
        expected = self.dashboards[0]
        current = json.loads(json.dumps(expected))
        query = next(
            query
            for query in MODULE.dashboard_builder_queries(current)
            if query["groupBy"]
        )
        query["groupBy"][0]["fieldContext"] = "resource"

        with self.assertRaisesRegex(RuntimeError, "stored dashboard query drift"):
            MODULE.validate_live_dashboard(current, expected, environment="pre-dev")

    def test_live_alert_contract_rejects_an_added_having_clause(self) -> None:
        expected = self.alerts[0]
        current = json.loads(json.dumps(expected))
        current["condition"]["compositeQuery"]["queries"][0]["spec"]["having"] = {
            "expression": "A > 1000000"
        }

        with self.assertRaisesRegex(RuntimeError, "stored alert query drift"):
            MODULE.validate_live_alert(
                current,
                expected,
                environment="pre-dev",
                channel_name="oriso-alerts-slack",
            )

    def test_collector_freshness_uses_the_alert_evaluation_window(self) -> None:
        """The freshness probe must not look further back than the alert does."""
        staleness = next(
            alert
            for alert in self.alerts
            if alert.get("_orisoSlug") == "collector-staleness"
        )
        self.assertEqual(MODULE.eval_window_minutes(staleness), 5)
        self.assertEqual(
            MODULE.eval_window_minutes({"evaluation": {"spec": {"evalWindow": "2h"}}}),
            120,
        )
        with self.assertRaisesRegex(
            RuntimeError, "unsupported alert evaluation window"
        ):
            MODULE.eval_window_minutes({"evaluation": {"spec": {"evalWindow": ""}}})

    def test_route_contract_requires_a_route_for_each_managed_rule_id(self) -> None:
        routes = [
            {
                "kind": "rule",
                "name": "rule-1",
                "expression": 'threshold_name == "critical" && rule_id == "rule-1"',
                "channels": ["oriso-alerts-slack"],
            },
            {
                "kind": "rule",
                "name": "unrelated-rule",
                "expression": 'rule_id == "unrelated-rule"',
                "channels": ["oriso-alerts-slack"],
            },
        ]

        with self.assertRaisesRegex(RuntimeError, "rule-2"):
            MODULE.validate_managed_routes(
                routes,
                rule_ids=["rule-1", "rule-2"],
                channel_name="oriso-alerts-slack",
            )


if __name__ == "__main__":
    unittest.main()
