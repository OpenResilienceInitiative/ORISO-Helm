#!/usr/bin/env python3
"""Contracts for the opt-in E2E dashboards over the oriso-e2e test spans (ORISO-E2E#97)."""

from __future__ import annotations

import copy
import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
ASSETS = ROOT / "files" / "signoz"
SPEC = importlib.util.spec_from_file_location(
    "signoz_observability", ASSETS / "signoz_observability.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

# Attribute names the reporter really exports (ORISO-E2E tests/support/otelReporter.ts).
REPORTER_ATTRIBUTES = {
    "test.run.id",
    "test.case",
    "test.file",
    "test.id",
    "test.project",
    "test.commit",
    "test.status",
    "test.retry",
    "test.duration_ms",
    "test.flaky",
}


def materialize(include_e2e: bool, environment: str = "dev"):
    dashboards, alerts = MODULE.load_assets(ASSETS, include_e2e=include_e2e)
    return MODULE.materialize_assets(
        dashboards,
        alerts,
        environment=environment,
        cluster_name="oriso-dev",
        channel_name="oriso-alerts-slack",
    )


def e2e_only(dashboards):
    return [item for item in dashboards if "e2e" in item["name"]]


class E2eDashboardTest(unittest.TestCase):
    def test_off_unless_asked_for(self) -> None:
        dashboards, _ = materialize(False)
        self.assertEqual(len(dashboards), 3)
        self.assertEqual(e2e_only(dashboards), [])

    def test_opt_in_adds_exactly_the_two_dashboards(self) -> None:
        dashboards, alerts = materialize(True)
        self.assertEqual(
            {item["name"] for item in e2e_only(dashboards)},
            {"oriso-e2e-test-health-dev", "oriso-e2e-flaky-dev"},
        )
        MODULE.validate_asset_contract(
            dashboards,
            alerts,
            environment="dev",
            cluster_name="oriso-dev",
            channel_name="oriso-alerts-slack",
        )

    def test_one_of_the_two_alone_is_rejected(self) -> None:
        dashboards, alerts = materialize(True)
        partial = [item for item in dashboards if item["name"] != "oriso-e2e-flaky-dev"]
        with self.assertRaises(RuntimeError):
            MODULE.validate_asset_contract(
                partial,
                alerts,
                environment="dev",
                cluster_name="oriso-dev",
                channel_name="oriso-alerts-slack",
            )

    def test_every_query_reads_test_spans_scoped_to_the_environment(self) -> None:
        dashboards, _ = materialize(True)
        queries = [
            query for item in e2e_only(dashboards) for query in MODULE.dashboard_builder_queries(item)
        ]
        self.assertGreaterEqual(len(queries), 10)
        for query in queries:
            self.assertEqual(query["signal"], "traces")
            expression = query["filter"]["expression"]
            self.assertIn("deployment.environment = 'dev'", expression)
            self.assertIn("service.name = 'oriso-e2e'", expression)

    def test_queries_only_use_attributes_the_reporter_exports(self) -> None:
        dashboards, _ = materialize(True)
        used = set()
        for query in MODULE.dashboard_builder_queries(e2e_only(dashboards)):
            used.update(field["name"] for field in query["groupBy"] if field["name"].startswith("test."))
            expressions = [query["filter"]["expression"]]
            expressions.extend(aggregation["expression"] for aggregation in query["aggregations"])
            for expression in expressions:
                fields = re.sub(r"'(?:[^'\\]|\\.)*'", "", expression)
                used.update(re.findall(r"\btest\.[a-z0-9_.]+", fields))
        self.assertTrue(used, "expected test.* attributes")
        self.assertEqual(used - REPORTER_ATTRIBUTES, set())

    def test_flaky_means_pass_and_fail_on_the_same_commit(self) -> None:
        dashboards, _ = materialize(True)
        flaky = next(item for item in dashboards if item["name"] == "oriso-e2e-flaky-dev")
        query = MODULE.dashboard_panel_queries(flaky)["flaky-by-commit"][0]
        self.assertEqual(
            {item["name"] for item in query["groupBy"]},
            {"test.id", "test.project", "test.file", "test.case", "test.commit"},
        )
        self.assertEqual(
            query["filter"]["expression"],
            "deployment.environment = 'dev' AND service.name = 'oriso-e2e'"
            " AND test.id EXISTS AND test.id != ''"
            " AND test.project EXISTS AND test.project != ''"
            " AND test.commit REGEXP '^[0-9a-fA-F]{40}$'"
            " AND test.status IN ('passed', 'failed', 'timedOut')",
        )
        self.assertEqual(
            [(item["alias"], item["expression"]) for item in query["aggregations"]],
            [
                ("passes", "countIf(test.status = 'passed' AND status_code = 1)"),
                ("fails", "countIf(test.status IN ('failed', 'timedOut') AND status_code = 2)"),
            ],
        )
        self.assertEqual(query["having"]["expression"], "passes > 0 AND fails > 0")

    def test_flaky_view_lists_every_seed_with_first_seen(self) -> None:
        dashboards, _ = materialize(True)
        flaky = next(item for item in dashboards if item["name"] == "oriso-e2e-flaky-dev")
        titles = [
            panel["spec"]["display"]["name"]
            for panel_id, panel in flaky["spec"]["panels"].items()
            if panel_id.startswith("seed-")
        ]
        for seed in (
            "ORISO-Frontend#1613",
            "ORISO-Frontend#1707",
            "ORISO-Frontend#1443",
            "ORISO-UserService#251",
            "ORISO-Admin#1016",
        ):
            self.assertEqual(sum(seed in title for title in titles), 1, seed)
        selectors = {
            "seed-1": "test.file = 'src/components/chatStage/ChatStage.stories.tsx'",
            "seed-2": (
                "test.file = 'src/practice/PracticeFlow.integration.test.tsx'"
                " AND test.case IN ('banner Restart mid-flow: same guard, fresh case with the accept available again, tour back at step 1',"
                " 'F1 without the team step (Träger switched it off): six steps, no team anchor, the same real actions')"
            ),
            "seed-3": (
                "test.file = 'src/components/chatStage/ChatStage.stories.tsx'"
                " AND test.case = '(r) New messages — writing: the arrow lights up'"
            ),
            "seed-4": (
                "test.file = 'src/test/java/de/caritas/cob/userservice/api/service/statistics/event/"
                "ArchiveOrDeleteSessionStatisticsEventTest.java'"
            ),
            "seed-5": (
                "test.file = 'src/pages/Agency/Edit/index.test.tsx'"
                " AND test.case = 'AgencyPageEdit no-topic activation confirm'"
            ),
        }
        panels = MODULE.dashboard_panel_queries(flaky)
        self.assertEqual(set(selectors), {key for key in panels if key.startswith("seed-")})
        for panel_id, selector in selectors.items():
            display = flaky["spec"]["panels"][panel_id]["spec"]["display"]
            self.assertIn("not seen", display["name"])
            self.assertIn("selected time range", display["description"])
            query = panels[panel_id][0]
            self.assertEqual(
                query["filter"]["expression"],
                "deployment.environment = 'dev' AND service.name = 'oriso-e2e' AND " + selector,
            )
            self.assertEqual(query["aggregations"][0]["expression"], "min(timestamp)")

    def test_tables_use_the_scalar_query_kind(self) -> None:
        dashboards, _ = materialize(True)
        for dashboard in e2e_only(dashboards):
            for panel in dashboard["spec"]["panels"].values():
                plugin = panel["spec"]["plugin"]["kind"]
                kind = panel["spec"]["queries"][0]["kind"]
                self.assertEqual(
                    (plugin, kind) in {("signoz/TablePanel", "scalar"), ("signoz/TimeSeriesPanel", "time_series")},
                    True,
                    (plugin, kind),
                )

    def test_a_drifted_stored_query_is_detected(self) -> None:
        dashboards, _ = materialize(True)
        expected = next(item for item in dashboards if item["name"] == "oriso-e2e-flaky-dev")
        MODULE.validate_live_dashboard(copy.deepcopy(expected), expected, environment="dev")
        drifted = copy.deepcopy(expected)
        MODULE.dashboard_panel_queries(drifted)  # shape check
        query = MODULE.dashboard_builder_queries(drifted["spec"]["panels"]["flaky-by-commit"])[0]
        query["having"]["expression"] = "passes > 0"
        with self.assertRaisesRegex(RuntimeError, "query drift"):
            MODULE.validate_live_dashboard(drifted, expected, environment="dev")

    def test_unbounded_grouping_is_still_refused(self) -> None:
        dashboards, alerts = materialize(True)
        bad = copy.deepcopy(dashboards)
        query = MODULE.dashboard_builder_queries(e2e_only(bad)[0])[0]
        query["groupBy"].append({"name": "test.stack", "fieldContext": "attribute", "fieldDataType": "string"})
        with self.assertRaisesRegex(RuntimeError, "unbounded grouping"):
            MODULE.validate_asset_contract(
                bad,
                alerts,
                environment="dev",
                cluster_name="oriso-dev",
                channel_name="oriso-alerts-slack",
            )


if __name__ == "__main__":
    unittest.main()
