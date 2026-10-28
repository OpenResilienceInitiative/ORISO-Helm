"""A native MAIL receipt needs the actual five unique cases, not only XML totals."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import create_autospec, patch
from types import SimpleNamespace
import xml.etree.ElementTree as ET

SCRIPT = Path(__file__).with_name("scenario_c_mail_native.py")
spec = importlib.util.spec_from_file_location("mail_native_evidence", SCRIPT)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
BROKER = "realNativeCanonicalPurposeIsAcceptedByTheActualBroker(TaskPurpose)"
JOINED = "sameNativeBrokerBearerReadsRealSavedCtsAppliesSmtpAndAcknowledgesExactRevision"


def smtp_fixture_call(base, method, path, body=None, token=None, context=None, form=False):
    """The actual pinned Keycloak SMTP fixture HTTP boundary signature."""


class MailEvidenceTest(unittest.TestCase):
    def test_reused_evidence_refuses_before_source_or_native_work_and_preserves_files(self):
        for populated in ("previous.json", "native-xml"):
            with self.subTest(populated=populated), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                checkout = root / "checkout"
                checkout.mkdir()
                (checkout / ".git").write_text("gitdir: unused-fixture")
                evidence = root / "evidence"
                evidence.mkdir()
                existing = evidence / populated
                if populated == "native-xml":
                    existing.mkdir()
                    existing = existing / "previous.xml"
                existing.write_text("retained previous evidence")
                argv = [str(SCRIPT), "--keycloak", str(checkout), "--cts", str(checkout),
                        "--user", str(checkout), "--java-home", str(root), "--evidence", str(evidence)]
                with patch.object(native.sys, "argv", argv), patch.object(native, "EVIDENCE", None), \
                        patch.object(native, "sources", side_effect=RuntimeError("source inspection reached")) as sources, \
                        patch.object(native, "run") as run:
                    with self.assertRaisesRegex(RuntimeError, "evidence directory must be empty"):
                        native.main()
                    sources.assert_not_called()
                    run.assert_not_called()
                self.assertEqual("retained previous evidence", existing.read_text())
                self.assertEqual([existing.relative_to(evidence)],
                                 [p.relative_to(evidence) for p in evidence.rglob("*") if p.is_file()])

    def test_empty_evidence_allows_normal_source_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            (checkout / ".git").write_text("gitdir: unused-fixture")
            evidence = root / "evidence"
            argv = [str(SCRIPT), "--keycloak", str(checkout), "--cts", str(checkout),
                    "--user", str(checkout), "--java-home", str(root), "--evidence", str(evidence)]
            with patch.object(native.sys, "argv", argv), patch.object(native, "EVIDENCE", None), \
                    patch.object(native, "sources", side_effect=RuntimeError("source inspection reached")) as sources, \
                    patch.object(native, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "source inspection reached"):
                    native.main()
                sources.assert_called_once_with(native.ROOT)
                run.assert_not_called()
            self.assertEqual([], list(evidence.iterdir()))

    def readback(self, replies):
        fixture = SimpleNamespace(call=create_autospec(smtp_fixture_call, side_effect=replies), REALM="fixture", INSTALLER="synthetic-installer-fixture")
        case = SimpleNamespace(base="https://native.fixture.invalid", installer="original-fixture-token")
        with patch.object(native, "case_module", fixture, create=True), patch.object(native, "EVIDENCE", None):
            value = native.read_smtp_state(case)
        return value, fixture, case

    def test_current_installer_readback_needs_no_new_token(self):
        expected = {"smtpServer": {"host": "smtp.fixture.internal"}}
        actual, fixture, _ = self.readback([(200, expected)])
        self.assertEqual(expected, actual)
        self.assertEqual(1, fixture.call.call_count)
        self.assertEqual("original-fixture-token", fixture.call.call_args.kwargs["token"])

    def test_expired_disposable_installer_token_is_renewed_once_after_the_real_join(self):
        expected = {"smtpServer": {"host": "smtp.fixture.internal"}}
        actual, fixture, case = self.readback([(401, {}), (200, {"access_token": "fresh-fixture-token"}), (200, expected)])
        self.assertEqual(expected, actual)
        self.assertEqual("fresh-fixture-token", case.installer)
        self.assertEqual(["GET", "POST", "GET"], [call.args[1] for call in fixture.call.call_args_list])
        self.assertEqual("/realms/master/protocol/openid-connect/token", fixture.call.call_args_list[1].args[2])

    def test_forbidden_readback_cannot_renew_or_become_a_green_receipt(self):
        fixture = SimpleNamespace(call=create_autospec(smtp_fixture_call, return_value=(403, {})), REALM="fixture")
        with patch.object(native, "case_module", fixture, create=True), patch.object(native, "EVIDENCE", None):
            with self.assertRaises(RuntimeError):
                native.read_smtp_state(SimpleNamespace(base="https://native.fixture.invalid", installer="original-fixture-token"))
        self.assertEqual(1, fixture.call.call_count)

    def test_missing_or_rejected_fresh_token_never_uses_the_expired_one_again(self):
        for reply in [(403, {}), (200, {}), (200, {"access_token": ""})]:
            with self.subTest(reply=reply), self.assertRaises(RuntimeError):
                self.readback([(401, {}), reply])

    def test_second_unauthorized_readback_is_not_retried_forever(self):
        with self.assertRaises(RuntimeError):
            self.readback([(401, {}), (200, {"access_token": "fresh-fixture-token"}), (401, {})])

    def report(self, change=None):
        root = ET.Element("testsuite", tests="5", failures="0", errors="0", skipped="0")
        for name in [BROKER + "[1]", BROKER + "[2]", BROKER + "[3]", JOINED, "realDeliveryBearerCannotApplySmtpEvenWithValidSharedKeyProof"]:
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

    def test_all_five_unique_real_cases_are_required(self):
        self.assertEqual({"tests": 5, "failures": 0, "errors": 0, "skipped": 0}, self.check(self.report()))

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
