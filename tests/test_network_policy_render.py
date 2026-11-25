"""Keep the chart's MatrixRTC isolation after removing the unused frontend policy."""

import subprocess
import unittest
from pathlib import Path

import yaml


CHART = Path(__file__).resolve().parents[1]
MATRIXRTC_POLICIES = ("matrixrtc-auth-policy-gateway", "matrixrtc-authorization-service")


class NetworkPolicyRenderTest(unittest.TestCase):
    def test_base_and_dev_keep_matrixrtc_policies_without_legacy_selector(self):
        for overlay in (None, "values-dev.yaml"):
            with self.subTest(overlay=overlay or "base"):
                values = [
                    "values.yaml.default",
                    "secrets.yaml.default",
                    "tests/fixtures/values-render-domain.yaml",
                    "tests/fixtures/render-required-secrets.yaml",
                ]
                if overlay:
                    values.append(overlay)
                args = ["helm", "template", "caritas", str(CHART), "--namespace", "caritas"]
                for source in values:
                    args.extend(["-f", str(CHART / source)])
                args.extend(["--set-string", "global.emailLegalOrganisationName=Render Test Foundation"])
                result = subprocess.run(args, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(
                    "io.kompose.network/frontend-network" in result.stdout,
                    "render contains the obsolete Kompose selector",
                )

                policies = {
                    doc["metadata"]["name"]: doc["spec"]
                    for doc in yaml.safe_load_all(result.stdout)
                    if doc and doc["kind"] == "NetworkPolicy"
                }
                self.assertNotIn("frontend-network", policies)
                for name in MATRIXRTC_POLICIES:
                    self.assertEqual(policies[name]["podSelector"], {"matchLabels": {"app": name}})
