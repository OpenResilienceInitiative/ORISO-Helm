"""A native MAIL receipt needs the actual four unique cases, not only XML totals."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

SCRIPT = Path(__file__).with_name("scenario_c_mail_native.py")
spec = importlib.util.spec_from_file_location("mail_native_evidence", SCRIPT)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
BROKER = "realNativeFiveAudienceMailIsAcceptedByTheActualBroker(TaskPurpose)"
JOINED = "sameNativeBrokerBearerReadsRealSavedCtsAppliesSmtpAndAcknowledgesExactRevision"


class MailEvidenceTest(unittest.TestCase):
    def report(self, change=None):
        root = ET.Element("testsuite", tests="4", failures="0", errors="0", skipped="0")
        for name in [BROKER + "[1]", BROKER + "[2]", BROKER + "[3]", JOINED]:
            ET.SubElement(root, "testcase", classname="de.caritas.cob.userservice.api.config.auth.RealMailActorNativeGate", name=name)
        if change:
            change(root)
        return root

    def check(self, root):
        with tempfile.TemporaryDirectory() as directory:
            user = Path(directory) / "user"
            reports = user / "target/surefire-reports"
            reports.mkdir(parents=True)
            ET.ElementTree(root).write(reports / "TEST-RealMailActorNativeGate.xml")
            evidence = Path(directory) / "evidence"
            evidence.mkdir()
            return native.strict_report(user, 0, evidence)

    def test_all_four_unique_real_cases_are_required(self):
        self.assertEqual({"tests": 4, "failures": 0, "errors": 0, "skipped": 0}, self.check(self.report()))

    def test_a_missing_case_cannot_hide_behind_the_summary(self):
        value = self.report()
        value.remove(value[-1])
        with self.assertRaises(RuntimeError):
            self.check(value)

    def test_duplicate_cases_cannot_replace_a_real_case(self):
        value = self.report()
        value[-1].set("name", value[0].get("name"))
        with self.assertRaises(RuntimeError):
            self.check(value)

    def test_unrelated_green_case_cannot_replace_the_actual_join(self):
        value = self.report()
        value[-1].set("name", "unrelatedGreenCase")
        with self.assertRaises(RuntimeError):
            self.check(value)

    def test_case_failures_errors_and_skips_override_a_green_summary(self):
        for marker in ("failure", "error", "skipped"):
            with self.subTest(marker=marker):
                value = self.report()
                ET.SubElement(value[0], marker)
                with self.assertRaises(RuntimeError):
                    self.check(value)

    def test_a_foreign_class_cannot_supply_a_missing_native_case(self):
        value = self.report()
        value[-1].set("classname", "ForeignFixture")
        with self.assertRaises(RuntimeError):
            self.check(value)

    def test_declared_failed_totals_also_refuse(self):
        value = self.report()
        value.set("errors", "1")
        with self.assertRaises(RuntimeError):
            self.check(value)
