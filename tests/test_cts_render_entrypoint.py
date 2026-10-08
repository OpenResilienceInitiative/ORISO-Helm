"""The CTS render contract must execute when CI runs its file directly."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


CONTRACT = pathlib.Path(__file__).resolve().parent / "render_cts_smtp_encryption_secret_test.py"


class CtsRenderEntrypointTest(unittest.TestCase):
    def test_direct_invocation_runs_both_contract_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp)
            calls = work / "helm-calls"
            fake_helm = work / "helm"
            fake_helm.write_text(
                "#!/bin/sh\n"
                'printf "called\\n" >> "$CTS_HELM_CALL_LOG"\n'
                'echo "consultingTypeService.smtpPasswordEncryptionSecret" >&2\n'
                "exit 42\n"
            )
            fake_helm.chmod(0o755)

            result = subprocess.run(
                [sys.executable, str(CONTRACT)],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PATH": f"{work}{os.pathsep}{os.environ['PATH']}",
                    "CTS_HELM_CALL_LOG": str(calls),
                },
            )

            # Four invalid-key renders should run before the valid-key case,
            # where the fake Helm must make the contract fail.
            self.assertTrue(calls.exists(), "direct invocation never called Helm")
            self.assertEqual(calls.read_text().count("called\n"), 5)
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
