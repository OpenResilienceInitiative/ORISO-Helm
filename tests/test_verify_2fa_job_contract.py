"""Properties of the 2FA verification Job that hold in the template source.

The render tests next door are the stronger check, but they need `helm` on PATH.
These read the template as text so they also run where helm is absent — which is
where this chart's changes are usually written. They can only see what is
structurally visible without rendering; anything value-dependent belongs in
`render_keycloak_verify_2fa_job_test.py`.
"""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "keycloak-verify-2fa-job.yaml"
VALUES = ROOT / "values.yaml.default"


class VerifyTwoFactorJobContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = TEMPLATE.read_text()
        cls.values = VALUES.read_text()

    def test_the_keycloak_address_is_not_hardcoded(self):
        # An environment whose Keycloak does not answer at the in-cluster default
        # would otherwise have the job check the wrong endpoint, and a check
        # pointed at nothing reports far more confidently than it knows.
        env_block = self.source.split("command:")[0]
        self.assertIn("adminUrl", env_block)

        hardcoded = re.search(
            r'value:\s*"http://keycloak\.\{\{ \.Release\.Namespace \}\}:8080/auth"',
            env_block,
        )
        self.assertIsNone(
            hardcoded, "KEYCLOAK_URL must come from a value, not a literal"
        )

    def test_no_endpoint_literal_survives_in_the_template(self):
        # A default host in the template is still a hardcoded host: it is the value
        # that actually ships, and an operator looking for it reads values, not Go
        # templates. The whole endpoint lives in values.yaml.default; the template
        # only resolves it.
        # An endpoint literal is a URL. "keycloak." on its own also appears in the
        # values path global.keycloak.*, which is not an address.
        for literal in ("http://", "https://", ":8080"):
            self.assertNotIn(
                literal, self.source, f"endpoint literal {literal!r} left in template"
            )

    def test_the_default_endpoint_stays_inside_the_cluster(self):
        # global.keycloak.authServerUrl is the PUBLIC url. A hook that leaves the
        # cluster to come back in fails whenever the ingress is not up yet, which
        # during a deploy is precisely when this job runs.
        self.assertNotIn("authServerUrl", self.source)
        self.assertIn('adminUrl: "http://keycloak.{{ .Release.Namespace }}', self.values)

    def test_an_empty_admin_url_fails_the_render_instead_of_guessing(self):
        # Falling back to a built-in address would put the literal back and would
        # point the check at an endpoint nobody chose.
        self.assertIn("required", self.source)

    def test_the_wait_for_keycloak_is_bounded(self):
        # `until ... sleep 5` with no limit outlives the release: helm's timeout is
        # client-side and never terminates the hook pod, and before-hook-creation
        # only removes it when the NEXT deploy runs. A wrong secret key would leave
        # a pod spinning until someone notices.
        self.assertIn("MAX_ATTEMPTS", self.source)
        self.assertRegex(self.source, r"ATTEMPTS.*-ge.*MAX_ATTEMPTS")

    def test_the_job_carries_its_own_deadline(self):
        # Belt to the loop's braces: a hang anywhere else in the script is still
        # bounded by kubernetes itself.
        self.assertIn("activeDeadlineSeconds:", self.source)

    def test_a_missing_check_script_can_be_made_fatal(self):
        # Skipping is right while the rolled-out image predates the script, and
        # wrong forever after: a job that always exits 0 reads exactly like a
        # passing check and buys confidence it has not earned.
        self.assertIn("requireCheckScript", self.source)
        self.assertIn("requireCheckScript", self.values)

    def test_the_check_still_never_writes_to_the_realm(self):
        for mutating in ("kcadm.sh create", "kcadm.sh update", "kcadm.sh delete"):
            self.assertNotIn(mutating, self.source)


if __name__ == "__main__":
    unittest.main()
