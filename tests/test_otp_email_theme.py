import json
import subprocess
import unittest
from pathlib import Path

import yaml


CHART_ROOT = Path(__file__).resolve().parents[1]


class OtpEmailThemeTest(unittest.TestCase):
    def test_rendered_keycloak_uses_only_the_image_email_theme(self):
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "otp-email-test",
                str(CHART_ROOT),
                "-f",
                str(CHART_ROOT / "values.yaml.default"),
                "-f",
                str(CHART_ROOT / "tests" / "fixtures" / "values-render-domain.yaml"),
                "-f",
                str(CHART_ROOT / "secrets.yaml.default"),
                "-f",
                str(CHART_ROOT / "tests" / "fixtures" / "render-required-secrets.yaml"),
                "--set-string",
                "tenantService.smtpPasswordEncryptionSecret=render-test-secret",
                "--set-string",
                "consultingTypeService.smtpPasswordEncryptionSecret=render-test-secret",
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
        realm_config = next(
            document
            for document in manifests
            if document.get("kind") == "ConfigMap"
            and document.get("metadata", {}).get("name")
            == "keycloak-configmap-data"
        )
        realm = json.loads(realm_config["data"]["realm.json"])
        self.assertEqual(realm["emailTheme"], "oriso")

        deployment = next(
            document
            for document in manifests
            if document.get("kind") == "Deployment"
            and document.get("metadata", {}).get("name")
            == "keycloak"
        )
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        mount_paths = {mount["mountPath"] for mount in container["volumeMounts"]}
        self.assertIn("/opt/keycloak/themes/custom-theme/login", mount_paths)
        self.assertFalse(
            any(path.startswith("/opt/keycloak/themes/custom-theme/email") for path in mount_paths)
        )
        volume_names = {
            volume["name"] for volume in deployment["spec"]["template"]["spec"]["volumes"]
        }
        self.assertFalse(any(name.startswith("keycloak-theme-email-") for name in volume_names))
        configmap_names = {
            document.get("metadata", {}).get("name", "")
            for document in manifests
            if document.get("kind") == "ConfigMap"
        }
        self.assertFalse(any(name.startswith("keycloak-configmap-theme-email-") for name in configmap_names))


if __name__ == "__main__":
    unittest.main()
