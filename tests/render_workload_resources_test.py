#!/usr/bin/env python3
"""Verify opt-in container resources and preserve database claim requests."""

import json
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
VALUES = {
    "userservice": "userService.resources",
    "mariadb": "online-counseling-mariadb.resources",
    "mongodb": "online-counseling-mongodb.resources",
}


def render(overrides):
    command = [
        "helm", "template", "workload-resources", str(ROOT),
        "-f", str(ROOT / "values.yaml.default"),
        "-f", str(ROOT / "secrets.yaml.default"),
        "-f", str(ROOT / "tests/fixtures/values-render-domain.yaml"),
        "-f", str(ROOT / "tests/fixtures/render-required-secrets.yaml"),
        "--set-string", "global.secrets.redisdefaultPass=test-redis-password",
        "--set-string", "userService.smtpUser=smtp-validation-user",
        "--set-string", "userService.smtpPassword=smtp-validation-password",
    ]
    for name, resources in overrides.items():
        command.extend(["--set-json", f"{VALUES[name]}={json.dumps(resources)}"])
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return {
        item["metadata"]["name"]: item["spec"]
        for item in yaml.safe_load_all(result.stdout)
        if item
        and item.get("kind") in {"Deployment", "StatefulSet"}
        and item["metadata"]["name"] in VALUES
    }


def pod(workloads, name):
    return workloads[name]["template"]["spec"]


def container(workloads, name):
    return next(item for item in pod(workloads, name)["containers"] if item["name"] == name)


class WorkloadResourcesTest(unittest.TestCase):
    def test_defaults_request_measured_resources_without_limits(self):
        # Baseline requests from Dev measurements (#583); limits stay unset.
        workloads = render({})
        expected = {
            "userservice": {"requests": {"cpu": "100m", "memory": "1536Mi"}},
            "mariadb": {"requests": {"cpu": "50m", "memory": "320Mi"}},
            "mongodb": {"requests": {"cpu": "150m", "memory": "448Mi"}},
        }
        for name, requirements in expected.items():
            with self.subTest(workload=name):
                self.assertEqual(container(workloads, name).get("resources"), requirements)

    def test_explicit_empty_values_preserve_pod_templates(self):
        workloads = render(dict.fromkeys(VALUES, {}))
        self.assertEqual(container(workloads, "userservice")["resources"], {})
        for name in ("mariadb", "mongodb"):
            self.assertNotIn("resources", container(workloads, name))

    def test_resources_stay_on_the_selected_containers(self):
        expected = {
            "userservice": {"requests": {"cpu": "200m", "memory": "512Mi"}},
            "mariadb": {
                "requests": {"cpu": "300m", "memory": "768Mi"},
                "limits": {"cpu": "1", "memory": "2Gi"},
            },
            "mongodb": {
                "requests": {"cpu": "400m", "memory": "1Gi"},
                "limits": {"cpu": "2", "memory": "3Gi"},
            },
        }
        baseline = render({})
        adjusted = render(expected)
        for name, requirements in expected.items():
            with self.subTest(workload=name):
                self.assertEqual(container(adjusted, name).get("resources"), requirements)
        for name in ("mariadb", "mongodb"):
            self.assertEqual(adjusted[name]["volumeClaimTemplates"], baseline[name]["volumeClaimTemplates"])
        self.assertEqual(pod(adjusted, "mongodb")["initContainers"], pod(baseline, "mongodb")["initContainers"])


if __name__ == "__main__":
    unittest.main()
