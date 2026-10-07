"""The real receiver gate must not credit a stale, missing or skipped suite."""

import os
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

    @staticmethod
    def write_report(receiver, **counts):
        reports = receiver / "target/surefire-reports"
        reports.mkdir(parents=True, exist_ok=True)
        path = reports / "TEST-synthetic.RealTaskTokenAuthorizationIT.xml"
        attrs = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, **counts}
        path.write_text("<testsuite " + " ".join(f'{key}="{value}"' for key, value in attrs.items()) + "/>")
        return path
