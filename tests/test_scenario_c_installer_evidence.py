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
    def test_owned_cgroup_snapshot_retains_only_numeric_memory_fields(self):
        identifier = "a" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc, cgroup = root / "proc", root / "cgroup"
            (proc / "123").mkdir(parents=True)
            (proc / "123" / "cgroup").write_text("0::/system.slice/docker-" + identifier + ".scope\n")
            memory = cgroup / "system.slice" / ("docker-" + identifier + ".scope")
            memory.mkdir(parents=True)
            for name, value in {"current": "7", "peak": "9", "max": "10",
                                "events": "low 0\nhigh 0\nmax 6\noom 0\noom_kill 0\n"}.items():
                (memory / ("memory." + name)).write_text(value)
            class Owned:
                label_key, run_id = "owned-run", "current"
                def inspect(self, kind, name):
                    return {"Id": identifier, "Config": {"Labels": {"owned-run": "current"}},
                            "State": {"Pid": 123}}
            snapshot = proof.owned_memory_snapshot(Owned(), "owned", proc, cgroup)
            self.assertEqual({"available": True, "currentBytes": 7, "peakBytes": 9,
                              "limitBytes": 10, "events": {"low": 0, "high": 0, "max": 6,
                                                             "oom": 0, "oom_kill": 0}}, snapshot)

    def test_foreign_ownership_is_refused_before_proc_or_cgroup_read(self):
        class Foreign:
            label_key, run_id = "owned-run", "current"
            def inspect(self, kind, name):
                return {"Config": {"Labels": {"owned-run": "other"}}}
        with patch.object(Path, "read_text", side_effect=AssertionError("No foreign reads")):
            self.assertEqual({"available": False, "failureType": "RuntimeError"},
                             proof.owned_memory_snapshot(Foreign(), "owned"))

    def test_wrong_cgroup_identity_cannot_be_reported_as_the_owned_peak(self):
        class Owned:
            label_key, run_id = "owned-run", "current"
            def inspect(self, kind, name):
                return {"Id": "a" * 64, "Config": {"Labels": {"owned-run": "current"}},
                        "State": {"Pid": 123}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "123").mkdir()
            (root / "123" / "cgroup").write_text("0::/system.slice/other.service\n")
            self.assertEqual({"available": False, "failureType": "RuntimeError"},
                             proof.owned_memory_snapshot(Owned(), "owned", root, root))

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

    def runtime(self):
        return [{"kind": "container", "name": "owned", "confirmedAbsent": True,
                 "anonymousVolumesConfirmedAbsent": [],
                 "runtime": {"oomKilled": False, "restartCount": 0}}]

    def groups(self):
        return [{"processGroup": 123, "confirmedAbsent": True}]

    def test_exact_runtime_and_owned_group_are_required(self):
        self.assertTrue(proof.runtime_accepted(self.runtime(), self.groups(), "owned"))
        for state in (None, {"oomKilled": None, "restartCount": 0},
                      {"oomKilled": True, "restartCount": 0},
                      {"oomKilled": False, "restartCount": None},
                      {"oomKilled": False, "restartCount": False},
                      {"oomKilled": False, "restartCount": 1}):
            receipt = self.runtime()
            receipt[0]["runtime"] = state
            self.assertFalse(proof.runtime_accepted(receipt, self.groups(), "owned"))

    def test_cleanup_missing_wrong_resource_and_surviving_group_refuse(self):
        self.assertFalse(proof.runtime_accepted([], self.groups(), "owned"))
        self.assertFalse(proof.runtime_accepted(self.runtime(), self.groups(), "foreign"))
        for groups in ([], self.groups() * 2, [{"processGroup": True, "confirmedAbsent": True}],
                       [{"processGroup": 0, "confirmedAbsent": True}],
                       [{"processGroup": 123, "confirmedAbsent": False}]):
            self.assertFalse(proof.runtime_accepted(self.runtime(), groups, "owned"))

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
