#!/usr/bin/env python3
"""Public CLI gate rejects stale, skipped and absent native receiver executions."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests/scenario_c_receiver_fixture.py"


class ReceiverReportGateTest(unittest.TestCase):
    def test_fresh_five_case_failsafe_report_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            receiver = Path(directory)
            reports = receiver / "target/failsafe-reports"
            reports.mkdir(parents=True)
            report = reports / "TEST-example.RealTaskTokenAuthorizationIT.xml"
            started = time.time() - 1
            report.write_text(
                '<testsuite name="example.RealTaskTokenAuthorizationIT" tests="5" failures="0" errors="0" skipped="0"/>'
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--verify-report",
                    str(receiver),
                    "--started",
                    str(started),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["tests"], 5)

    def test_stale_skipped_empty_duplicate_and_legacy_reports_are_rejected(self):
        cases = (
            "stale",
            "skipped",
            "empty",
            "duplicate",
            "surefire-only",
            "missing",
            "malformed",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                receiver = Path(directory)
                folder = (
                    "target/surefire-reports"
                    if case == "surefire-only"
                    else "target/failsafe-reports"
                )
                reports = receiver / folder
                reports.mkdir(parents=True)
                report = reports / "TEST-example.RealTaskTokenAuthorizationIT.xml"
                started = time.time() + 1 if case == "stale" else time.time() - 1
                xml = '<testsuite tests="5" failures="0" errors="0" skipped="0"/>'
                if case == "skipped":
                    xml = '<testsuite tests="5" failures="0" errors="0" skipped="1"/>'
                if case == "empty":
                    xml = '<testsuite tests="0" failures="0" errors="0" skipped="0"/>'
                if case == "malformed":
                    xml = "<testsuite"
                if case != "missing":
                    report.write_text(xml)
                if case == "duplicate":
                    (
                        reports / "TEST-other.RealTaskTokenAuthorizationIT.xml"
                    ).write_text(xml)
                result = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "--verify-report",
                        str(receiver),
                        "--started",
                        str(started),
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("error", json.loads(result.stdout))

    def test_invalid_start_timestamp_cannot_claim_fresh_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            receiver = Path(directory)
            reports = receiver / "target/failsafe-reports"
            reports.mkdir(parents=True)
            (reports / "TEST-example.RealTaskTokenAuthorizationIT.xml").write_text(
                '<testsuite tests="5" failures="0" errors="0" skipped="0"/>'
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--verify-report",
                    str(receiver),
                    "--started",
                    "nan",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
