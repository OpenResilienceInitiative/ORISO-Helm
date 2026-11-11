"""scripts/observability_overlay.py takes over observability values only."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from observability_overlay import CHART_DIR, overlay  # noqa: E402


class ObservabilityOverlayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.values = overlay(CHART_DIR / "values-dev.yaml")

    def test_only_observability_keys(self) -> None:
        self.assertEqual(set(self.values), {"global", "signoz", "signozCollector", "dbMonitoring"})
        self.assertEqual(set(self.values["global"]), {"observability"})

    def test_environment_wins_over_defaults(self) -> None:
        observability = self.values["global"]["observability"]
        self.assertEqual(observability["deploymentEnvironment"], "dev")
        self.assertEqual(observability["tracingSamplingProbability"], "1.0")
        self.assertTrue(self.values["signoz"]["enabled"])
        self.assertIs(self.values["signoz"]["orisoIngress"]["devAliasRedirect"], True)
        self.assertEqual(self.values["signozCollector"]["clusterName"], "oriso-dev")

    def test_defaults_missing_from_an_old_base_are_carried(self) -> None:
        self.assertTrue(self.values["dbMonitoring"]["curlImage"].startswith("docker.io/curlimages/curl@sha256:"))
        self.assertTrue(self.values["signoz"]["orisoDataProtection"]["enabled"])

    def test_static_collector_policy_is_not_an_overlay_value(self) -> None:
        self.assertNotIn("config", self.values["signoz"]["otelCollector"])
        policy_path = CHART_DIR / "charts/signoz/files/oriso-otel-collector-policy.yaml"
        policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
        self.assertIn("otlp/rum", policy["receivers"])

    def check_effective(self, contents: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(CHART_DIR / "scripts" / "observability_overlay.py"),
                "values-dev.yaml",
                "--check-effective",
            ],
            input=contents,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_check_accepts_dev_overlay_and_unrelated_values(self) -> None:
        self.values["global"]["domainName"] = "dev.oriso.org"
        self.values["frontend"] = {"image": "example/dev:fixture"}
        result = self.check_effective(yaml.safe_dump(self.values))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_check_detects_lost_dev_overlay_without_printing_values(self) -> None:
        self.values["signozCollector"]["enabled"] = False
        self.values["global"]["observability"].pop("webVitals")
        self.values["signoz"]["clickhouse"]["resources"]["limits"]["memory"] = "DO-NOT-PRINT"
        result = self.check_effective(yaml.safe_dump(self.values))
        self.assertEqual(result.returncode, 1)
        self.assertIn("signozCollector.enabled", result.stderr)
        self.assertIn("global.observability.webVitals", result.stderr)
        self.assertIn("signoz.clickhouse.resources.limits.memory", result.stderr)
        self.assertNotIn("DO-NOT-PRINT", result.stderr + result.stdout)

    def test_check_rejects_invalid_input_without_echoing_it(self) -> None:
        result = self.check_effective("secret-value: [DO-NOT-PRINT")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Cannot read effective Helm values", result.stderr)
        self.assertNotIn("DO-NOT-PRINT", result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
