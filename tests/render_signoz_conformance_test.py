#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def render(*extra_values: Path) -> list[dict]:
    command = [
        "helm",
        "template",
        "oriso-platform",
        str(ROOT),
        "--namespace",
        "caritas",
        "-f",
        str(ROOT / "values.yaml.default"),
        "-f",
        str(ROOT / "secrets.yaml.default"),
        "--set",
        "signoz.enabled=true",
    ]
    for path in extra_values:
        command.extend(["-f", str(path)])
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [
        item for item in yaml.safe_load_all(result.stdout) if isinstance(item, dict)
    ]


def resource(documents: list[dict], kind: str, name: str) -> dict:
    return next(
        item
        for item in documents
        if item.get("kind") == kind and item.get("metadata", {}).get("name") == name
    )


class SignozConformanceRenderTest(unittest.TestCase):
    def test_all_signal_pipelines_receive_environment_identity(self) -> None:
        documents = render()
        gateway = resource(
            documents, "ConfigMap", "oriso-platform-otel-collector"
        )
        gateway_config = yaml.safe_load(
            gateway["data"]["otel-collector-config.yaml"]
        )
        environment = gateway_config["processors"]["resource/environment"][
            "attributes"
        ][0]

        self.assertEqual(environment["key"], "deployment.environment")
        self.assertEqual(environment["value"], "pre-dev")
        self.assertEqual(environment["action"], "upsert")
        for pipeline in gateway_config["service"]["pipelines"].values():
            self.assertIn("resource/environment", pipeline["processors"])

        agent = resource(documents, "ConfigMap", "oriso-platform-otel-agent")
        agent_config = yaml.safe_load(agent["data"]["otel-agent-config.yaml"])
        for pipeline in agent_config["service"]["pipelines"].values():
            self.assertIn("resource/environment", pipeline["processors"])
        self.assertIn(
            "resource/infrastructure",
            agent_config["service"]["pipelines"]["metrics"]["processors"],
        )

        production = render(ROOT / "values-prod.yaml")
        production_gateway = resource(
            production, "ConfigMap", "oriso-platform-otel-collector"
        )
        production_config = yaml.safe_load(
            production_gateway["data"]["otel-collector-config.yaml"]
        )
        self.assertEqual(
            production_config["processors"]["resource/environment"]["attributes"][
                0
            ]["value"],
            "production",
        )

    def test_log_parser_sets_canonical_service_and_trace_context(self) -> None:
        documents = render()
        agent = resource(documents, "ConfigMap", "oriso-platform-otel-agent")
        agent_config = yaml.safe_load(agent["data"]["otel-agent-config.yaml"])
        operators = agent_config["receivers"]["filelog"]["operators"]

        json_parser = next(item for item in operators if item["type"] == "json_parser")
        trace_parser = next(
            item for item in operators if item["type"] == "trace_parser"
        )
        self.assertEqual(json_parser["parse_from"], "body")
        self.assertEqual(trace_parser["trace_id"]["parse_from"], "attributes.traceId")
        self.assertEqual(trace_parser["span_id"]["parse_from"], "attributes.spanId")

        transform = agent_config["processors"]["transform/log_identity"]
        statements = json.dumps(transform)
        for canonical in (
            "user-service",
            "agency-service",
            "tenant-service",
            "consulting-type-service",
        ):
            self.assertIn(canonical, statements)

    def test_clickhouse_internal_logs_have_ttl_and_reconciliation_job(self) -> None:
        documents = render()
        config = resource(
            documents, "ConfigMap", "oriso-platform-clickhouse-config"
        )["data"]["internal-logs.xml"]
        for table in (
            "query_log",
            "text_log",
            "trace_log",
            "metric_log",
            "asynchronous_metric_log",
        ):
            self.assertIn(f"<{table}>", config)
            self.assertIn("<ttl>event_date + INTERVAL 3 DAY DELETE</ttl>", config)

        job = resource(
            documents, "Job", "oriso-platform-clickhouse-retention"
        )
        command = json.dumps(job["spec"]["template"]["spec"])
        for table in (
            "system.query_log",
            "system.text_log",
            "system.trace_log",
            "system.metric_log",
            "system.asynchronous_metric_log",
        ):
            self.assertIn(table, command)
        self.assertNotIn("MATERIALIZE TTL", command)

    def test_contract_names_required_live_proof(self) -> None:
        contract = yaml.safe_load(
            (ROOT / "observability/signoz-conformance/contract.yaml").read_text()
        )
        self.assertEqual(contract["deploymentEnvironment"], "pre-dev")
        self.assertEqual(
            set(contract["webVitals"]["services"]), {"frontend", "admin"}
        )
        self.assertLessEqual(contract["webVitals"]["maxAgeMinutes"], 30)
        self.assertTrue(contract["correlation"]["requireLogToTrace"])
        self.assertTrue(contract["backup"]["required"])
        self.assertLessEqual(contract["capacity"]["maxDiskUsedPercent"], 75)
        self.assertEqual(
            set(contract["alerts"]["required"]),
            {
                "matrix_notification_exception_rate",
                "redis_availability_store_failure",
                "provisioning_rollback_failure",
                "deployment_identity_mismatch",
                "clickhouse_capacity_warning",
            },
        )
        for dashboard in contract["dashboards"]:
            self.assertTrue(dashboard["requiredDimensions"])
            self.assertTrue(dashboard["liveQuery"])


if __name__ == "__main__":
    unittest.main()
