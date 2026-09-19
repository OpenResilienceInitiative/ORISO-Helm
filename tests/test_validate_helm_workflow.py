import re
import unittest

import yaml
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "validate-helm-chart.yml"
REQUIREMENTS = ROOT / "requirements-ci.txt"


class ValidateHelmWorkflowContractTest(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_third_party_actions_are_immutable_and_checkout_is_read_only(self):
        action_refs = re.findall(r"^\s*uses:\s*([^\s#]+)", self.workflow, re.MULTILINE)
        self.assertGreaterEqual(len(action_refs), 3)
        for action_ref in action_refs:
            with self.subTest(action_ref=action_ref):
                self.assertRegex(action_ref, r"@[0-9a-f]{40}$")
        self.assertIn("persist-credentials: false", self.workflow)

    def test_python_cache_and_install_share_the_pinned_manifest(self):
        self.assertTrue(REQUIREMENTS.is_file())
        self.assertEqual("PyYAML==6.0.3\n", REQUIREMENTS.read_text(encoding="utf-8"))
        self.assertIn("cache-dependency-path: requirements-ci.txt", self.workflow)
        self.assertIn(
            "python -m pip install --disable-pip-version-check -r requirements-ci.txt",
            self.workflow,
        )
        self.assertNotRegex(self.workflow, r"pip install[^\n]*\spyyaml(?:\s|$)")

    def test_the_gate_covers_every_branch_that_is_deployed_from(self):
        """The validation must fire on the branches a deployment can come from.

        This gate was removed from dev on 2026-09-03 and only ever pushed on
        `pre-dev`, `dev` and `master`. `master` does not exist in this
        repository, so the push trigger never fired for the default branch --
        and `main` is what Staging is built from. A chart could therefore reach
        Staging without ever being rendered, linted or checked against the 40
        contract tests in tests/.
        """
        triggers = yaml.safe_load(self.workflow)
        # PyYAML parses the bare key `on` as the boolean True.
        events = triggers.get("on") or triggers.get(True)
        self.assertIn("pull_request", events)
        branches = events["push"]["branches"]
        self.assertIn("dev", branches)
        self.assertIn("main", branches)
        self.assertNotIn(
            "master", branches, "master is not a branch in this repository"
        )

    def test_a_reviewed_chart_must_be_packagable(self):
        lint_position = self.workflow.index("- name: Lint chart")
        package_position = self.workflow.index("- name: Package chart")
        self.assertGreater(package_position, lint_position)
        self.assertIn(
            "helm package . --destination /tmp/oriso-chart-package", self.workflow
        )


if __name__ == "__main__":
    unittest.main()
