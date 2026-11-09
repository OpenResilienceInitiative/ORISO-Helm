#!/usr/bin/env python3
"""Contracts for the opt-in E2E and CI test dashboards (ORISO-E2E#97)."""

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


# Attributes used from the CI exporter in ORISO-E2E #117.
CI_REPORTER_ATTRIBUTES = {
    "test.repository", "test.framework", "test.project", "test.id",
    "test.file", "test.case", "test.commit", "test.job.id", "test.status",
    "test.evidence.origin", "test.evidence.observed_at_ms",
}
DATE_COLUMN = {"A.min(test.evidence.observed_at_ms)": "dateTimeAsIso"}


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

    def test_runtime_and_ci_queries_use_distinct_scopes(self) -> None:
        dashboards, alerts = materialize(True)
        ci_panels = set()
        for dashboard in e2e_only(dashboards):
            for panel, (query,) in MODULE.dashboard_panel_queries(dashboard).items():
                expression = query["filter"]["expression"]
                if query["signal"] == "logs":
                    ci_panels.add(panel)
                    self.assertIn("service.name = 'oriso-ci-tests'", expression)
                    self.assertIn("test.evidence.origin = 'github-actions'", expression)
                    self.assertIn("test.status IN ('passed', 'failed')", expression)
                    self.assertIn("test.evidence.observed_at_ms > 0", expression)
                    self.assertIn("test.job.id EXISTS", expression)
                    self.assertIn("test.id EXISTS", expression)
                    self.assertIn("test.project IN ('unit', 'storybook (chromium)')", expression)
                    self.assertNotIn("deployment.environment", expression)
                else:
                    self.assertEqual(query["signal"], "traces")
                    self.assertIn("deployment.environment = 'dev'", expression)
                    self.assertIn("service.name = 'oriso-e2e'", expression)
        self.assertEqual(ci_panels, {
            "ci-pass-rate", "ci-flaky-by-commit", "seed-1", "seed-2", "seed-3", "seed-4", "seed-5",
        })
        core = copy.deepcopy(dashboards)
        MODULE.dashboard_builder_queries(core[0])[0]["signal"] = "logs"
        with self.assertRaisesRegex(RuntimeError, "CI log query"):
            MODULE.validate_asset_contract(core, alerts, environment="dev",
                                           cluster_name="oriso-dev", channel_name="oriso-alerts-slack")

    def test_queries_only_use_attributes_their_reporter_exports(self) -> None:
        dashboards, _ = materialize(True)
        used = {"traces": set(), "logs": set()}
        for query in MODULE.dashboard_builder_queries(e2e_only(dashboards)):
            fields_used = used[query["signal"]]
            fields_used.update(field["name"] for field in query["groupBy"] if field["name"].startswith("test."))
            expressions = [query["filter"]["expression"]]
            expressions.extend(aggregation["expression"] for aggregation in query["aggregations"])
            for expression in expressions:
                fields = re.sub(r"'(?:[^'\\]|\\.)*'", "", expression)
                fields_used.update(re.findall(r"\btest\.[a-z0-9_.]+", fields))
        self.assertTrue(used["traces"] and used["logs"], "expected attributes for both signals")
        self.assertEqual(used["traces"] - REPORTER_ATTRIBUTES, set())
        self.assertEqual(used["logs"] - CI_REPORTER_ATTRIBUTES, set())

    def test_ci_health_and_flaky_detection_deduplicate_job_outcomes(self) -> None:
        dashboards, _ = materialize(True)
        health, flaky = e2e_only(dashboards)
        rate, = MODULE.dashboard_panel_queries(health)["ci-pass-rate"]
        mixed, = MODULE.dashboard_panel_queries(flaky)["ci-flaky-by-commit"]
        runs = "count_distinct(test.job.id)"
        failed = "count_distinct_if(test.job.id, test.status = 'failed')"
        passed = "count_distinct_if(test.job.id, test.status = 'passed')"
        groups = ["test.repository", "test.project", "test.id", "test.file", "test.case"]
        self.assertEqual([item["name"] for item in rate["groupBy"]], groups)
        self.assertEqual([item["name"] for item in mixed["groupBy"]], groups + ["test.commit"])
        self.assertEqual(rate["aggregations"], [
            {"expression": f"({runs} - {failed}) * 100.0 / {runs}", "alias": "pass_rate_pct"},
            {"expression": runs, "alias": "jobs"},
        ])
        self.assertEqual(mixed["aggregations"], [
            {"expression": f"{runs} - {failed}", "alias": "passes"},
            {"expression": f"{runs} - {passed}", "alias": "fails"},
            {"expression": "min(test.evidence.observed_at_ms)", "alias": "first_seen"},
        ])
        self.assertEqual(mixed["having"]["expression"], "passes > 0 AND fails > 0")
        self.assertIn("test.commit REGEXP '^[0-9a-f]{40}$'", mixed["filter"]["expression"])

    def test_health_counts_distinct_first_runs_by_test_identity_and_project(self) -> None:
        dashboards, _ = materialize(True)
        health = next(item for item in dashboards if item["name"] == "oriso-e2e-test-health-dev")
        panels = MODULE.dashboard_panel_queries(health)
        expected_filter = (
            "deployment.environment = 'dev' AND service.name = 'oriso-e2e'"
            " AND test.run.id EXISTS AND test.run.id != ''"
            " AND test.project EXISTS AND test.project != ''"
            " AND test.retry = 0 AND test.status IN ('passed', 'failed', 'timedOut')"
            " AND status_code IN (1, 2)"
        )
        for panel in ("pass-rate", "failing"):
            query = panels[panel][0]
            self.assertEqual(query["filter"]["expression"], expected_filter)
            self.assertEqual(
                [field["name"] for field in query["groupBy"]],
                ["test.project", "test.id", "test.file", "test.case"],
            )
            self.assertEqual(query["aggregations"][-1], {
                "expression": "count_distinct(test.run.id)", "alias": "runs",
            })

        self.assertEqual(panels["pass-rate"][0]["aggregations"][0], {
            "expression": (
                "(count_distinct(test.run.id) - count_distinct_if(test.run.id, status_code = 2))"
                " * 100.0 / count_distinct(test.run.id)"
            ),
            "alias": "pass_rate_pct",
        })
        self.assertEqual(panels["failing"][0]["aggregations"][0], {
            "expression": "count_distinct_if(test.run.id, status_code = 2)",
            "alias": "unexpected_runs",
        })
        self.assertEqual(panels["failing"][0]["having"]["expression"], "unexpected_runs > 0")
        description = health["spec"]["panels"]["pass-rate"]["spec"]["display"]["description"]
        self.assertIn("legacy", description)

    def test_recent_runs_label_attempt_rows_and_exclude_unbound_spans(self) -> None:
        dashboards, _ = materialize(True)
        health = next(item for item in dashboards if item["name"] == "oriso-e2e-test-health-dev")
        query = MODULE.dashboard_panel_queries(health)["runs"][0]
        self.assertEqual(
            query["filter"]["expression"],
            "deployment.environment = 'dev' AND service.name = 'oriso-e2e'"
            " AND test.run.id EXISTS AND test.run.id != '' AND test.retry EXISTS AND test.status EXISTS",
        )
        self.assertEqual(
            [(item["alias"], item["expression"]) for item in query["aggregations"]],
            [("started", "min(timestamp)"), ("attempt_rows", "count()"),
             ("unexpected_attempt_rows", "countIf(status_code = 2)")],
        )

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
        admin_confirmation_cases = (
            "shows the confirm dialog and does not save when going live without a topic",
            "keeps the form values and does not save when the no-topic dialog is cancelled",
            "saves offline:false and empty topicIds when the no-topic dialog is confirmed",
            "saves without a dialog when going live with a topic selected",
            "does not warn when an already-online agency is saved without topics",
            "shows the confirm dialog when an existing offline agency goes live without a topic",
            "keeps the registration card in edit mode when the no-topic dialog is cancelled",
            "saves the registration card online when the no-topic dialog is confirmed",
            "saves without a dialog when an existing agency has a persisted topic",
        )
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
                " AND test.case = 'getPayload_Should_ReturnValidJsonPayload'"
            ),
            "seed-5": (
                "test.file = 'src/pages/Agency/Edit/index.test.tsx'"
                " AND test.case IN (" + ", ".join(f"'{case}'" for case in admin_confirmation_cases) + ")"
            ),
        }
        identities = {
            "seed-1": ("Frontend", "storybook (chromium)"),
            "seed-2": ("Frontend", "unit"),
            "seed-3": ("Frontend", "storybook (chromium)"),
            "seed-4": ("UserService", "unit"),
            "seed-5": ("Admin", "unit"),
        }
        panels = MODULE.dashboard_panel_queries(flaky)
        self.assertEqual(set(selectors), {key for key in panels if key.startswith("seed-")})
        for panel_id, selector in selectors.items():
            with self.subTest(panel_id=panel_id):
                spec = flaky["spec"]["panels"][panel_id]["spec"]
                self.assertIn("First Seen", spec["display"]["name"])
                self.assertIn("selected time range", spec["display"]["description"])
                repository, project = identities[panel_id]
                query, = panels[panel_id]
                self.assertEqual(query["signal"], "logs")
                self.assertTrue(query["filter"]["expression"].endswith(
                    f" AND (test.repository = 'OpenResilienceInitiative/ORISO-{repository}'"
                    f" AND test.project = '{project}' AND {selector})"
                ))
                self.assertEqual(query["aggregations"], [
                    {"expression": "min(test.evidence.observed_at_ms)", "alias": "first_seen"},
                    {"expression": "count_distinct(test.job.id)", "alias": "jobs"},
                ])
                self.assertEqual(spec["plugin"]["spec"]["formatting"]["columnUnits"], DATE_COLUMN)

    def test_stored_date_format_drift_is_detected(self) -> None:
        dashboards, _ = materialize(True)
        expected = next(item for item in dashboards if item["name"] == "oriso-e2e-flaky-dev")
        drifted = copy.deepcopy(expected)
        drifted["spec"]["panels"]["flaky-retry"]["spec"]["plugin"]["spec"]["formatting"] = {
            "columnUnits": None, "decimalPrecision": "2",
        }
        MODULE.validate_live_dashboard(drifted, expected, environment="dev")
        drifted["spec"]["panels"]["seed-4"]["spec"]["plugin"]["spec"].clear()
        with self.assertRaisesRegex(RuntimeError, "formatting drift"):
            MODULE.validate_live_dashboard(drifted, expected, environment="dev")

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
