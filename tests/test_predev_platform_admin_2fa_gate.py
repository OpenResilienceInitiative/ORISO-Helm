import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-predev-platform-admin-2fa.sh"


FAKE_KUBECTL = r"""#!/usr/bin/env bash
set -euo pipefail
joined="$*"

if [[ "$joined" == *"get configmap"* ]]; then
  printf '%s' "${CONFIG_VALUE:-true}"
elif [[ "$joined" == *"range .spec.template.spec.containers[0].env"* ]]; then
  if [[ "${SCENARIO:-healthy}" != "missing-ref" ]]; then
    printf '%s\n' 'IDENTITY_OTP_ALLOWED_FOR_TENANT_SUPER_ADMINS=userservice-configmap-env:IDENTITY_OTP_ALLOWED_FOR_TENANT_SUPER_ADMINS'
  fi
elif [[ "$joined" == *"rollout status"* ]]; then
  printf '%s\n' 'deployment successfully rolled out'
elif [[ "$joined" == *"exec deployment/"* ]]; then
  printf '%s' "${POD_VALUE:-true}"
elif [[ "$joined" == *"kubernetes"*"revision"* ]]; then
  printf '%s' '386'
elif [[ "$joined" == *"containers[0].image"* ]]; then
  printf '%s' 'ghcr.io/openresilienceinitiative/oriso-userservice@sha256:6792b0cb1de46c841a228e9153fcbe8506f5486020c766919b09e13f8f163898'
else
  printf 'unexpected kubectl invocation: %s\n' "$joined" >&2
  exit 99
fi
"""


class PlatformAdminTwoFactorRuntimeGateTest(unittest.TestCase):
    def run_gate(self, **environment: str) -> subprocess.CompletedProcess[str]:
        self.assertTrue(SCRIPT.is_file(), "runtime gate script is missing")
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "kubectl"
            fake.write_text(textwrap.dedent(FAKE_KUBECTL), encoding="utf-8")
            fake.chmod(0o755)
            return subprocess.run(
                [str(SCRIPT)],
                cwd=ROOT,
                env={**os.environ, "KUBECTL_BIN": str(fake), **environment},
                text=True,
                capture_output=True,
                check=False,
            )

    def test_passes_when_config_deployment_and_pod_agree(self) -> None:
        result = self.run_gate()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("platform-admin 2FA runtime gate: PASS", result.stdout)
        self.assertIn("revision=386", result.stdout)
        self.assertIn("@sha256:", result.stdout)

    def test_fails_when_deployment_does_not_import_the_config_key(self) -> None:
        result = self.run_gate(SCENARIO="missing-ref")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not import", result.stderr)
        self.assertIn("IDENTITY_OTP_ALLOWED_FOR_TENANT_SUPER_ADMINS", result.stderr)

    def test_fails_when_effective_pod_value_is_not_true(self) -> None:
        result = self.run_gate(POD_VALUE="false")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("effective pod value is 'false'", result.stderr)


if __name__ == "__main__":
    unittest.main()
