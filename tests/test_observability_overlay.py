"""scripts/observability_overlay.py takes over observability values only."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

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
        self.assertEqual(self.values["signozCollector"]["clusterName"], "oriso-dev")

    def test_defaults_missing_from_an_old_base_are_carried(self) -> None:
        self.assertTrue(self.values["dbMonitoring"]["curlImage"].startswith("docker.io/curlimages/curl@sha256:"))
        self.assertTrue(self.values["signoz"]["orisoDataProtection"]["enabled"])

    def test_explicit_null_survives_to_delete_upstream_keys(self) -> None:
        receivers = self.values["signoz"]["otelCollector"]["config"]["receivers"]
        self.assertIn("jaeger", receivers)
        self.assertIsNone(receivers["jaeger"])


if __name__ == "__main__":
    unittest.main()
