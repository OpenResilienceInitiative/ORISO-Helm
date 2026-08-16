import subprocess
import unittest
from pathlib import Path

import yaml


CHART_ROOT = Path(__file__).resolve().parents[1]


class OtpEmailThemeTest(unittest.TestCase):
    def test_rendered_keycloak_email_theme_keeps_otp_readable_and_selectable(self):
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "otp-email-test",
                str(CHART_ROOT),
                "-f",
                str(CHART_ROOT / "values.yaml.default"),
                "-f",
                str(CHART_ROOT / "secrets.yaml.default"),
                "--set-string",
                "userService.smtpUser=smtp-test-user",
                "--set-string",
                "userService.smtpPassword=smtp-test-password",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        manifests = [
            document
            for document in yaml.safe_load_all(rendered.stdout)
            if isinstance(document, dict)
        ]
        email_theme = next(
            document
            for document in manifests
            if document.get("kind") == "ConfigMap"
            and document.get("metadata", {}).get("name")
            == "keycloak-configmap-theme-email-html"
        )
        template = email_theme["data"]["otp-email.ftl"]

        self.assertIn("<html lang=\"${locale.language}\">", template)
        self.assertIn(">ORISO</td>", template)
        self.assertIn("aria-label=\"${kcSanitize(msg(\"emailCodeAriaLabel\", otp))?no_esc}\"", template)
        self.assertIn("user-select:all", template)
        self.assertIn("${kcSanitize(msg(\"emailCopyHint\"))?no_esc}", template)
        self.assertNotIn("data:image", template)
        self.assertNotIn("<script", template)
        self.assertNotIn("onclick=", template)

        messages = next(
            document
            for document in manifests
            if document.get("kind") == "ConfigMap"
            and document.get("metadata", {}).get("name")
            == "keycloak-configmap-theme-email-messages"
        )
        self.assertIn("emailHeading=Ihr 2FA-Code", messages["data"]["messages_de.properties"])
        self.assertIn("emailHeading=Your 2FA code", messages["data"]["messages_en.properties"])


if __name__ == "__main__":
    unittest.main()
