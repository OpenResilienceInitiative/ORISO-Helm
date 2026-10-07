"""Fresh installs keep platform SMTP in Admin Settings, not chart values."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

import yaml


CHART = Path(__file__).resolve().parents[1]
SMTP_KEYS = {
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_SECURE",
    "SMTP_FROM",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_REQUIRED",
}


class AdminManagedPlatformSmtpTest(unittest.TestCase):
    def test_fresh_install_without_smtp_chart_values(self) -> None:
        self.assert_admin_managed_smtp(
            "userService.smtpHost=",
            "userService.smtpFrom=",
            "userService.smtpPort=",
            "userService.smtpSecure=",
        )

    def test_legacy_chart_values_do_not_become_a_runtime_source(self) -> None:
        self.assert_admin_managed_smtp(
            "userService.smtpHost=smtp.legacy.internal",
            "userService.smtpFrom=sender@legacy.internal",
            "userService.smtpUser=legacy-user",
            "userService.smtpPassword=legacy-test-password",
        )

    def assert_admin_managed_smtp(self, *overrides: str) -> None:
        args = [
            "helm",
            "template",
            "admin-smtp-install-test",
            str(CHART),
            "-f",
            str(CHART / "values.yaml.default"),
            "-f",
            str(CHART / "tests/fixtures/values-render-domain.yaml"),
            "-f",
            str(CHART / "secrets.yaml.default"),
            "-f",
            str(CHART / "tests/fixtures/render-required-secrets.yaml"),
        ]
        for override in overrides:
            args += ["--set-string", override]

        result = subprocess.run(args, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]
        config = next(
            doc for doc in docs if doc.get("metadata", {}).get("name") == "userservice-configmap-env"
        )
        secret = next(
            doc for doc in docs if doc.get("metadata", {}).get("name") == "userservice-secret"
        )
        deployment = next(
            doc for doc in docs if doc.get("metadata", {}).get("name") == "userservice"
            and doc.get("kind") == "Deployment"
        )
        env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]

        self.assertTrue(SMTP_KEYS.isdisjoint(config["data"]))
        self.assertTrue(SMTP_KEYS.isdisjoint(secret["data"]))
        self.assertTrue(SMTP_KEYS.isdisjoint(entry["name"] for entry in env))
        self.assertTrue(config["data"]["CONSULTING_TYPE_SERVICE_API_URL"])
        client_secret = next(doc for doc in docs if doc.get("metadata", {}).get("name") == "keycloak-backend-client-secrets")
        self.assertTrue(client_secret["data"]["KEYCLOAK_BACKEND_ADMIN_CLIENT_SECRET"])
        self.assertTrue(client_secret["data"]["KEYCLOAK_BACKEND_TECHNICAL_CLIENT_SECRET"])


if __name__ == "__main__":
    unittest.main()
