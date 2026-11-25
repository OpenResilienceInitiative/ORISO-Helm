"""Contract for the independently executed Dev HTTPS probe."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import ssl
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location(
    "signoz_external_uptime",
    Path(__file__).parents[1] / "scripts" / "signoz_external_uptime.py",
)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class SigNozExternalUptimeTest(unittest.TestCase):
    now = 1_795_000_000

    def setUp(self):
        self.context = Mock(spec=ssl.SSLContext)
        self.alias = probe.Response(302, probe.CANONICAL_URL, "", b"", self.now + 21 * 86400)
        self.ui = probe.Response(200, None, "text/html", b"", self.now + 40 * 86400)
        self.health = probe.Response(200, None, "application/json", b'{"status":"ok"}', self.now + 40 * 86400)

    def check(self, alias=None, ui=None, health=None):
        with patch.object(probe, "get", side_effect=[
            alias if alias is not None else self.alias,
            ui if ui is not None else self.ui,
            health if health is not None else self.health,
        ]) as get:
            result = probe.check(self.context, now=self.now)
        self.assertEqual(
            [(call.args[0], call.args[1]) for call in get.call_args_list],
            [("signoz.oriso.org", "/"), ("dev.oriso.org", "/signoz/"),
             ("dev.oriso.org", "/signoz/api/v1/health?live=1")],
        )
        return result

    def test_healthy_at_exactly_twenty_one_days(self):
        result = self.check()
        self.assertEqual(result["state"], "OK")
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["certificates"]["alias"]["seconds_remaining"], 21 * 86400)

    def test_expiry_warning_and_failure_have_distinct_severity(self):
        alias = self.alias._replace(expires_at=self.now + 21 * 86400 - 1)
        self.assertEqual(self.check(alias=alias)["state"], "WARNING")
        self.assertEqual(self.check(alias=alias, health=self.health._replace(status=503))["state"], "CRITICAL")

    def test_earliest_canonical_certificate_decides_severity(self):
        health = self.health._replace(expires_at=self.now + 7 * 86400)
        self.assertEqual(self.check(health=health)["state"], "WARNING")
        result = self.check(health=health._replace(expires_at=health.expires_at - 1))
        self.assertEqual(result["state"], "CRITICAL")
        self.assertEqual(result["issues"], ["canonical_certificate_expires_within_7_days"])
        self.assertEqual(result["certificates"]["canonical"]["seconds_remaining"], 7 * 86400 - 1)

    def test_wrong_redirect_is_rejected_without_following_it(self):
        for location in (
            "http://dev.oriso.org/signoz/",
            "https://dev.oriso.org.example.invalid/signoz/",
            "https://dev.oriso.org/signoz/?unexpected=1",
            None,
        ):
            with self.subTest(location=location):
                result = self.check(alias=self.alias._replace(location=location))
                self.assertEqual(result["state"], "CRITICAL")
                self.assertIn("alias_redirect", result["issues"])

    def test_tls_failure_and_non_200_ui_are_reported_independently(self):
        result = self.check(
            alias=ssl.SSLCertVerificationError("untrusted-sensitive-server-text"),
            ui=self.ui._replace(status=502),
        )
        self.assertEqual(result["state"], "CRITICAL")
        self.assertEqual(result["issues"], ["alias_tls", "ui_status"])
        self.assertNotIn("untrusted-sensitive-server-text", json.dumps(result))

    def test_health_rejects_html_and_invalid_or_oversized_json(self):
        for replacement in (
            {"content_type": "text/html"},
            {"body": b'{"status":"down"}'},
            {"body": b'<html>Login</html>'},
            {"body": b'["ok"]'},
            {"body": self.health.body + b" " * 4096},
        ):
            with self.subTest(replacement=replacement.keys()):
                self.assertEqual(self.check(health=self.health._replace(**replacement))["state"], "CRITICAL")

    def test_cli_exit_status_matches_check_result(self):
        for expected, state in enumerate(("OK", "WARNING", "CRITICAL", "UNKNOWN")):
            with self.subTest(state=state), contextlib.redirect_stdout(io.StringIO()) as output:
                with patch.object(ssl, "create_default_context"), patch.object(
                    probe, "check", return_value={"state": state},
                ):
                    self.assertEqual(probe.main([]), expected)
                self.assertEqual(json.loads(output.getvalue())["state"], state)


if __name__ == "__main__":
    unittest.main()
