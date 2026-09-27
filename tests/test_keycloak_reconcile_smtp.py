"""Verify the Keycloak SMTP update command without a live realm."""

import os
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "files" / "keycloak-reconcile-smtp.sh.in"


class ReconcileSmtpTest(unittest.TestCase):
    def test_starttls_sender_and_secret_handling(self):
        with tempfile.TemporaryDirectory() as tmp:
            kcadm = Path(tmp) / "kcadm"
            log = Path(tmp) / "calls"
            kcadm.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$FAKE_LOG\"\ncat > \"$FAKE_BODY\"\n")
            kcadm.chmod(0o700)
            body = Path(tmp) / "body"
            env = {
                "PATH": os.environ["PATH"], "KCADM": str(kcadm), "FAKE_LOG": str(log), "FAKE_BODY": str(body),
                "KEYCLOAK_REALM": "online-beratung", "SMTP_HOST": "smtp.canary.example",
                "SMTP_PORT": "587", "SMTP_SECURE": "false",
                "SMTP_FROM": "ORISO Platform <sender@canary.example>",
                "SMTP_USER": "canary-user", "SMTP_PASSWORD": "canary-password",
            }
            result = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("canary-password", result.stdout + result.stderr)
            args = log.read_text().splitlines()
            self.assertEqual(args, ["update", "realms/online-beratung", "-f", "-"])
            smtp = json.loads(body.read_text())["smtpServer"]
            self.assertEqual(smtp["host"], "smtp.canary.example")
            self.assertEqual(smtp["starttls"], "true")
            self.assertEqual(smtp["ssl"], "false")
            self.assertEqual(smtp["from"], "sender@canary.example")
            self.assertEqual(smtp["fromDisplayName"], "ORISO Platform")
            self.assertEqual(smtp["password"], "canary-password")

            env["SMTP_PASSWORD"] = 'quote" and backslash\\ test'
            result = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(body.read_text())["smtpServer"]["password"], env["SMTP_PASSWORD"])

            env["SMTP_PASSWORD"] = "bad\nsecret"
            result = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(env["SMTP_PASSWORD"], result.stderr)

            env["SMTP_PASSWORD"] = ""
            result = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SMTP_PASSWORD", result.stderr)


if __name__ == "__main__":
    unittest.main()
