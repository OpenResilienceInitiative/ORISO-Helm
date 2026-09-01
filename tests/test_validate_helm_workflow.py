import re
import unittest
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

    def test_the_vendored_k8s_infra_subchart_is_validated_on_its_own(self):
        """The root render gate runs with k8s-infra disabled, so lint it directly."""
        self.assertIn("- name: Validate vendored k8s-infra subchart", self.workflow)
        self.assertIn("helm lint charts/k8s-infra", self.workflow)
        self.assertIn("helm template k8s-infra-validation charts/k8s-infra", self.workflow)
        self.assertIn(
            "helm package charts/k8s-infra --destination /tmp/k8s-infra-package",
            self.workflow,
        )
        self.assertNotIn("helm upgrade --install", self.workflow)

    def test_a_reviewed_chart_must_be_packagable(self):
        lint_position = self.workflow.index("- name: Lint chart")
        package_position = self.workflow.index("- name: Package chart")
        self.assertGreater(package_position, lint_position)
        self.assertIn(
            "helm package . --destination /tmp/oriso-chart-package", self.workflow
        )


if __name__ == "__main__":
    unittest.main()
