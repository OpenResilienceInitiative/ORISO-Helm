#!/usr/bin/env python3
"""Contract tests for the chart release workflow's version resolution.

Three of the last six release runs on `main` failed on a single line:

    Chart.yaml version is 2.0.3, but workflow input is 2.0.4

Chart.yaml on main is the release intent — it got there through a reviewed
release PR. Re-typing the version into the dispatch form made a second,
unreviewed source of truth, and a release is exactly the moment where a
half-hour round trip over a typo hurts most.

The input is now optional. Empty means "release what the chart says"; a
supplied value still has to match, so an operator can assert the version they
believe they are shipping. These tests pin both halves of that, because the
logic lives in shell inside YAML where nothing else would catch a regression.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-helm-chart.yml"

# The resolution as the workflow performs it. Kept byte-identical to the
# workflow's own lines below by test_resolution_matches_the_workflow.
RESOLVE = 'VERSION="${EXPECTED:-$CHART_VERSION}"'


def resolve(expected: str, chart_version: str) -> str:
    script = f'EXPECTED={expected!r}; CHART_VERSION={chart_version!r}; {RESOLVE}; printf %s "$VERSION"'
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    ).stdout


class VersionResolution(unittest.TestCase):
    def test_empty_input_releases_the_chart_version(self):
        self.assertEqual(resolve("", "2.0.4"), "2.0.4")

    def test_a_supplied_version_is_used_as_the_expectation(self):
        self.assertEqual(resolve("2.0.3", "2.0.4"), "2.0.3")

    def test_a_matching_expectation_resolves_to_the_same_version(self):
        self.assertEqual(resolve("2.0.4", "2.0.4"), "2.0.4")


class WorkflowContract(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")
        parsed = yaml.safe_load(self.text)
        # PyYAML parses the bare key `on` as the boolean True.
        self.events = parsed.get("on") or parsed.get(True)

    def test_the_version_input_is_optional(self):
        version = self.events["workflow_dispatch"]["inputs"]["version"]
        self.assertFalse(
            version.get("required", False),
            "a required version input reintroduces the mismatch failure",
        )

    def test_resolution_matches_the_workflow(self):
        self.assertIn(RESOLVE, self.text)

    def test_a_mismatch_still_fails_the_release(self):
        """The guard's purpose — catching an unbumped Chart.yaml — must survive."""
        self.assertIn('if [[ "$CHART_VERSION" != "$VERSION" ]]; then', self.text)
        self.assertIn('if [[ "$APP_VERSION" != "$VERSION" ]]; then', self.text)

    def test_release_still_only_runs_from_main(self):
        self.assertIn('if [[ "${GITHUB_REF_NAME}" != "main" ]]; then', self.text)


if __name__ == "__main__":
    unittest.main()
