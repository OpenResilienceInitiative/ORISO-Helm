"""The real receiver gate must not credit a stale, missing or skipped suite."""

import os
import subprocess
import sys
from pathlib import Path
import tempfile
import time
import unittest

import task_receiver_permission_matrix as matrix


class ReceiverExecutionGateTest(unittest.TestCase):
    def test_fresh_executed_suite_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            receiver = Path(directory)
            started = time.time() - 1
            self.write_report(receiver, tests=4)
            self.assertEqual(
                matrix.executed_counts(receiver, started), {"tests": 4, "failures": 0, "errors": 0, "skipped": 0}
            )

    def test_missing_or_stale_report_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            receiver = Path(directory)
            started = time.time()
            with self.assertRaises(RuntimeError):
                matrix.executed_counts(receiver, started)
            path = self.write_report(receiver, tests=4)
            os.utime(path, (started - 60, started - 60))
            with self.assertRaises(RuntimeError):
                matrix.executed_counts(receiver, started)

    def test_zero_skipped_failed_or_errored_suite_is_not_success(self):
        for counts in (
            {"tests": 0},
            {"tests": 4, "skipped": 4},
            {"tests": 4, "failures": 1},
            {"tests": 4, "errors": 1},
        ):
            with self.subTest(counts=counts), tempfile.TemporaryDirectory() as directory:
                receiver = Path(directory)
                self.write_report(receiver, **counts)
                with self.assertRaises(RuntimeError):
                    matrix.executed_counts(receiver, time.time() - 1)

    def test_creation_suite_requires_all_five_fresh_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            receiver = Path(directory)
            self.write_report(receiver, suite_name="IdentityCreationNativeRestartIT", tests=5)
            self.assertEqual(
                matrix.executed_counts(receiver, time.time() - 1,
                    suite_name="IdentityCreationNativeRestartIT", expected_tests=5),
                {"tests": 5, "failures": 0, "errors": 0, "skipped": 0},
            )

    def test_creation_gate_rejects_dropped_failed_errored_or_skipped_cases(self):
        for counts in ({"tests": 4}, {"tests": 6}, {"tests": 5, "failures": 1},
                       {"tests": 5, "errors": 1}, {"tests": 5, "skipped": 1}):
            with self.subTest(counts=counts), tempfile.TemporaryDirectory() as directory:
                receiver = Path(directory)
                self.write_report(receiver, suite_name="IdentityCreationNativeRestartIT", **counts)
                with self.assertRaises(RuntimeError):
                    matrix.executed_counts(receiver, time.time() - 1,
                        suite_name="IdentityCreationNativeRestartIT", expected_tests=5)

    def test_creation_gate_rejects_missing_or_stale_report(self):
        with tempfile.TemporaryDirectory() as directory:
            receiver = Path(directory)
            started = time.time()
            with self.assertRaises(RuntimeError):
                matrix.executed_counts(receiver, started,
                    suite_name="IdentityCreationNativeRestartIT", expected_tests=5)
            path = self.write_report(receiver, suite_name="IdentityCreationNativeRestartIT", tests=5)
            os.utime(path, (started - 60, started - 60))
            with self.assertRaises(RuntimeError):
                matrix.executed_counts(receiver, started,
                    suite_name="IdentityCreationNativeRestartIT", expected_tests=5)

    def test_creation_cli_requires_pre_command_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            receiver = Path(directory)
            self.write_report(receiver, suite_name="IdentityCreationNativeRestartIT", tests=5)
            result = subprocess.run([sys.executable, matrix.__file__,
                "--verify-creation-report", str(receiver)], capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("--started", result.stderr)

    def test_creation_cli_accepts_exact_five_and_rejects_dropped_case(self):
        with tempfile.TemporaryDirectory() as directory:
            receiver = Path(directory)
            for count, expected_exit in [(5, 0), (4, 1)]:
                self.write_report(receiver, suite_name="IdentityCreationNativeRestartIT", tests=count)
                result = subprocess.run([sys.executable, matrix.__file__,
                    "--verify-creation-report", str(receiver), "--started", str(time.time() - 1)],
                    capture_output=True, text=True)
                self.assertEqual(expected_exit, result.returncode, result.stderr)

    @staticmethod
    def write_report(receiver, suite_name="RealTaskTokenAuthorizationIT", **counts):
        reports = receiver / "target/surefire-reports"
        reports.mkdir(parents=True, exist_ok=True)
        path = reports / ("TEST-synthetic." + suite_name + ".xml")
        attrs = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, **counts}
        path.write_text("<testsuite " + " ".join(f'{key}="{value}"' for key, value in attrs.items()) + "/>")
        return path
