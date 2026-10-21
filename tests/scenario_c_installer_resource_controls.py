"""Explicit finite installer timeout control; no JVM, Docker or real memory sample."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SUPPORT_ROOT = None


class InstallerTimeoutControl(unittest.TestCase):
    def test_deadline_refuses_and_the_owned_process_group_is_absent(self):
        script = SUPPORT_ROOT / "scripts/test-bootstrap-context-roundtrip.py"
        self.assertEqual(
            "0cf44ec7e99edaac9916f8f9dd2a17bca295e3348b8ab0fac2bb28c5a10e5949",
            hashlib.sha256(script.read_bytes()).hexdigest(),
        )
        spec = importlib.util.spec_from_file_location("installer_timeout_support", script)
        support = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(support)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            support.configure_memory(2896, root)
            with patch.object(support, "free_guard", return_value=90):
                with self.assertRaises(support.FixtureError):
                    support.bounded(
                        [sys.executable, "-c", "import time; time.sleep(60)"],
                        root, os.environ.copy(), root / "private.log", 0, time.monotonic() + 2,
                    )
            receipts = [json.loads(line) for line in (root / "process-cleanup.jsonl").read_text().splitlines()]
            self.assertEqual(1, len(receipts))
            self.assertTrue(receipts[0]["confirmedAbsent"])
            self.assertGreater(receipts[0]["processGroup"], 0)
            with self.assertRaises(ProcessLookupError):
                os.killpg(receipts[0]["processGroup"], 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--support-root", required=True, type=Path)
    arguments = parser.parse_args()
    SUPPORT_ROOT = arguments.support_root.resolve()
    unittest.main(argv=[sys.argv[0]], verbosity=1)
