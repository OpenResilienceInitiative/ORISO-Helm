"""Offline eligibility checks for the six-case native installer proof."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts/ci/scenario-c-installer-proof.py"
)
spec = importlib.util.spec_from_file_location("installer_proof", SCRIPT)
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


class InstallerEvidenceTest(unittest.TestCase):
    def complete(self):
        return {name: ["passed"] for name in proof.EXPECTED}

    def test_six_distinct_successes_are_eligible(self):
        self.assertTrue(proof.accepted(self.complete(), 6, 0))

    def test_missing_duplicate_or_substituted_case_is_not_eligible(self):
        for change in ("missing", "duplicate", "substituted"):
            with self.subTest(change=change):
                cases = self.complete()
                name = next(iter(cases))
                if change == "missing":
                    del cases[name]
                elif change == "duplicate":
                    cases[name].append("passed")
                else:
                    del cases[name]
                    cases["unrelated_success"] = ["passed"]
                self.assertFalse(proof.accepted(cases, 6, 0))

    def test_skip_failure_or_setup_error_cannot_be_green(self):
        for status in ("skipped", "failure", "error"):
            cases = self.complete()
            cases[next(iter(cases))] = [status]
            self.assertFalse(proof.accepted(cases, 6, 0))
        self.assertFalse(proof.accepted(self.complete(), 6, 1))

    def test_direct_cases_refuses_before_loading_native_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "result.json"
            with patch.dict(proof.os.environ, {}, clear=True):
                with self.assertRaises(RuntimeError):
                    proof.cases(report)
            self.assertFalse(report.exists())

    def test_stale_or_unbound_child_cannot_count_as_green(self):
        report = {"cases": self.complete(), "tests": 6, "errors": 0}
        self.assertFalse(proof.verified_child(report, "current", "head"))
        report.update(runId="previous", commit="head")
        self.assertFalse(proof.verified_child(report, "current", "head"))
        report.update(runId="current", commit="old-head")
        self.assertFalse(proof.verified_child(report, "current", "head"))
        report.update(commit="head")
        self.assertTrue(proof.verified_child(report, "current", "head"))

    def test_actual_failed_subtests_serialize_as_one_method(self):
        class Broken(unittest.TestCase):
            def test_two_failed_subtests(self):
                for number in range(2):
                    with self.subTest(number=number):
                        self.fail("private payload must not be retained")

        result = proof.SanitizedResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(Broken).run(result)
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "result.json"
            self.assertFalse(proof.serialize_result(result, report, "current", "head"))
            xml = ET.parse(report.with_suffix(".xml")).getroot()
            self.assertEqual("1", xml.attrib["tests"])
            self.assertEqual("1", xml.attrib["failures"])
            self.assertEqual(1, len(xml.findall("testcase")))
            self.assertEqual(
                2, json.loads(report.read_text())["subtestFailureObservations"]
            )
            self.assertNotIn("private payload", report.read_text())


if __name__ == "__main__":
    unittest.main()
