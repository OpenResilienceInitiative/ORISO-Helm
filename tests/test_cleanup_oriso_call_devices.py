#!/usr/bin/env python3
"""Behavior tests for the guarded ORISO_CALL_ device cleanup."""

from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "cleanup-oriso-call-devices.sh"
CONFIRMATION = "DELETE_DISPOSABLE_ORISO_CALL_DEVICES"


class CleanupOrisoCallDevicesTest(unittest.TestCase):
    def run_script(self, *arguments: str) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            users = root / "users.txt"
            users.write_text("@alice:matrix.example\n", encoding="utf-8")
            calls = root / "curl-calls.jsonl"
            fake_curl = root / "curl"
            fake_curl.write_text(
                """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_CURL_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
if "POST" not in args:
    print(json.dumps({"devices": [
        {"device_id": "ORISO_WEB_KEEP"},
        {"device_id": "ORISO_CALL_DELETE_1"},
        {"device_id": "ORISO_CALL_DELETE_2"}
    ]}))
else:
    print("{}")
""",
                encoding="utf-8",
            )
            fake_curl.chmod(fake_curl.stat().st_mode | stat.S_IXUSR)

            env = {
                **os.environ,
                "PATH": f"{root}:{os.environ['PATH']}",
                "FAKE_CURL_LOG": str(calls),
                "SYNAPSE_ADMIN_URL": "http://synapse.internal:8008",
                "SYNAPSE_ADMIN_TOKEN": "sentinel-admin-token",
            }
            result = subprocess.run(
                [str(SCRIPT), "--users-file", str(users), *arguments],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            logged_calls = [
                json.loads(line)
                for line in calls.read_text(encoding="utf-8").splitlines()
            ] if calls.exists() else []
            return result, logged_calls

    def test_dry_run_is_default_and_never_deletes(self) -> None:
        result, calls = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Dry-run: 2 disposable", result.stdout)
        self.assertIn("No devices were deleted", result.stdout)
        self.assertFalse(any("POST" in call for call in calls))
        self.assertNotIn("sentinel-admin-token", result.stdout + result.stderr)

    def test_apply_requires_exact_confirmation(self) -> None:
        result, calls = self.run_script("--apply")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(CONFIRMATION, result.stderr)
        self.assertEqual(calls, [])

    def test_apply_deletes_only_prefixed_devices(self) -> None:
        result, calls = self.run_script(
            "--apply",
            "--confirm",
            CONFIRMATION,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        posts = [call for call in calls if "POST" in call]
        self.assertEqual(len(posts), 1)
        payload = json.loads(posts[0][posts[0].index("--data") + 1])
        self.assertEqual(
            payload,
            {"devices": ["ORISO_CALL_DELETE_1", "ORISO_CALL_DELETE_2"]},
        )
        self.assertNotIn("ORISO_WEB_KEEP", json.dumps(payload))
        self.assertIn("Applied: 2 disposable", result.stdout)
        self.assertNotIn("sentinel-admin-token", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
