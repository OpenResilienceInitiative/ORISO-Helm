"""Deployments route traffic only after their application responds."""

import subprocess
import unittest
from pathlib import Path

import yaml


CHART = Path(__file__).resolve().parents[1]
PROBES = {
    "frontend": ("/beratung-hilfe.html", 8080),
    "agencyservice": ("/actuator/health/readiness", 8084),
    "consultingtypeservice": ("/actuator/health/readiness", 8083),
}


class WorkloadReadinessProbeTest(unittest.TestCase):
    def test_http_readiness_targets_each_application_listener(self):
        result = subprocess.run(
            [
                "helm", "template", "workload-readiness-test", str(CHART),
                "-f", str(CHART / "values.yaml.default"),
                "-f", str(CHART / "secrets.yaml.default"),
                "-f", str(CHART / "tests/fixtures/values-render-domain.yaml"),
                "-f", str(CHART / "tests/fixtures/render-required-secrets.yaml"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        resources = {
            (doc["kind"], doc["metadata"]["name"]): doc["spec"]
            for doc in yaml.safe_load_all(result.stdout)
            if doc and doc["kind"] in ("Deployment", "Service")
        }

        for name, (path, port) in PROBES.items():
            with self.subTest(workload=name):
                deployment = resources["Deployment", name]
                containers = deployment["template"]["spec"]["containers"]
                container = next(container for container in containers if container["name"] == name)
                probe = container.get("readinessProbe")
                self.assertIsNotNone(probe, "application has no readiness probe")
                self.assertEqual(probe["httpGet"], {"path": path, "port": port})

                service_ports = resources["Service", name]["ports"]
                self.assertIn(port, {service_port.get("targetPort") for service_port in service_ports})
