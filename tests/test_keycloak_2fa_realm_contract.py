import json
import unittest
from pathlib import Path


REALM_PATH = (
    Path(__file__).resolve().parents[1]
    / "charts"
    / "keycloak"
    / "keycloak-resources"
    / "realm.json"
)


class KeycloakTwoFactorRealmContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.realm = json.loads(REALM_PATH.read_text())
        cls.flows = {
            flow["alias"]: flow for flow in cls.realm["authenticationFlows"]
        }

    def flow(self, alias):
        self.assertIn(
            alias,
            self.flows.keys(),
            f"missing authentication flow {alias}",
        )
        return self.flows[alias]

    def test_custom_two_factor_flow_is_the_direct_grant_binding(self):
        self.assertEqual("direct-grant-2fa", self.realm["directGrantFlow"])

    def test_direct_grant_flow_requires_password_and_both_otp_subflows(self):
        executions = self.flow("direct-grant-2fa")["authenticationExecutions"]

        self.assertEqual(
            [
                ("direct-grant-validate-username", None, "REQUIRED"),
                ("direct-grant-validate-password", None, "REQUIRED"),
                (None, "app-otp-conditional", "CONDITIONAL"),
                (None, "email-otp-conditional", "CONDITIONAL"),
            ],
            [
                (
                    execution.get("authenticator"),
                    execution.get("flowAlias"),
                    execution["requirement"],
                )
                for execution in executions
            ],
        )

    def test_app_otp_subflow_returns_the_challenge_and_validates_totp(self):
        executions = self.flow("app-otp-conditional")["authenticationExecutions"]

        self.assertEqual(
            [
                "conditional-user-configured",
                "app-authenticator",
                "direct-grant-validate-otp",
            ],
            [execution["authenticator"] for execution in executions],
        )
        self.assertTrue(
            all(execution["requirement"] == "REQUIRED" for execution in executions)
        )

    def test_email_otp_subflow_uses_the_configured_email_authenticator(self):
        executions = self.flow("email-otp-conditional")["authenticationExecutions"]

        self.assertEqual(
            ["conditional-user-configured", "email-authenticator"],
            [execution["authenticator"] for execution in executions],
        )
        self.assertTrue(
            all(execution["requirement"] == "REQUIRED" for execution in executions)
        )
        self.assertEqual("email-otp-config", executions[1]["authenticatorConfig"])

        email_config = next(
            config
            for config in self.realm["authenticatorConfig"]
            if config["alias"] == "email-otp-config"
        )
        self.assertEqual(
            {
                "length": "6",
                "ttl": "900",
                "senderId": "Onlineberatung",
                "simulation": "false",
            },
            email_config["config"],
        )

    def test_browser_flow_asks_for_the_email_code_too(self):
        # Without this subflow the built-in `forms` flow carries auth-otp-form
        # alone, which knows only the app credential. For an e-mail-only user its
        # conditional-user-configured finds nothing configured, the subflow is
        # skipped, and the browser login completes on a password alone — no error,
        # no prompt, just a second factor that quietly is not one.
        executions = self.flow("forms")["authenticationExecutions"]
        subflows = [
            execution.get("flowAlias")
            for execution in executions
            if execution.get("authenticatorFlow")
        ]

        self.assertIn(
            "browser-email-otp-conditional",
            subflows,
            "the browser forms flow must carry the e-mail OTP subflow",
        )

    def test_browser_email_subflow_uses_the_browser_capable_authenticator(self):
        executions = self.flow("browser-email-otp-conditional")[
            "authenticationExecutions"
        ]

        self.assertEqual(
            ["conditional-user-configured", "email-form-authenticator"],
            [execution["authenticator"] for execution in executions],
        )
        self.assertTrue(
            all(execution["requirement"] == "REQUIRED" for execution in executions)
        )
        # Same config as the direct-grant twin: length, ttl and sender belong to the
        # realm's e-mail OTP, not to the surface asking for it. Two configs would let
        # a code be valid in the app and expired in the browser.
        self.assertEqual("email-otp-config", executions[1]["authenticatorConfig"])

    def test_the_direct_grant_and_browser_paths_use_different_authenticators(self):
        # email-authenticator answers with a JSON challenge a token client reads;
        # email-form-authenticator renders a page. Swapping them silently breaks
        # whichever surface gets the wrong one.
        direct = self.flow("email-otp-conditional")["authenticationExecutions"]
        browser = self.flow("browser-email-otp-conditional")["authenticationExecutions"]

        self.assertEqual("email-authenticator", direct[1]["authenticator"])
        self.assertEqual("email-form-authenticator", browser[1]["authenticator"])

    def test_technical_user_keeps_the_otp_spi_role(self):
        technical_user = next(
            user for user in self.realm["users"] if user["username"] == "technical"
        )

        self.assertIn("technical", technical_user["realmRoles"])

    def test_realm_uses_the_oriso_email_theme(self):
        self.assertEqual("oriso", self.realm.get("emailTheme"))


if __name__ == "__main__":
    unittest.main()
