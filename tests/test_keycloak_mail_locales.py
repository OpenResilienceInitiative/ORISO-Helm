import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


REALM_PATH = (
    Path(__file__).resolve().parents[1]
    / "charts"
    / "keycloak"
    / "keycloak-resources"
    / "realm.json"
)
ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "files" / "keycloak-reconcile-mail-locales.sh.in"


class KeycloakMailLocalesTest(unittest.TestCase):
    def test_realm_supports_every_app_mail_language(self):
        realm = json.loads(REALM_PATH.read_text())
        self.assertTrue(realm["internationalizationEnabled"])
        self.assertEqual(
            ["de", "en", "fr", "ru", "ti", "tr"],
            realm["supportedLocales"],
        )
        self.assertEqual("de", realm["defaultLocale"])
        self.assertEqual("oriso", realm["emailTheme"])

    def test_live_reconciler_only_changes_mail_locale_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory) / "calls"
            fake_kcadm = Path(directory) / "kcadm"
            fake_kcadm.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" >> "$CALLS"\n'
            )
            fake_kcadm.chmod(0o755)
            result = subprocess.run(
                ["sh", str(SCRIPT_PATH)],
                env={
                    **os.environ,
                    "KCADM": str(fake_kcadm),
                    "KEYCLOAK_REALM": "online-beratung",
                    "CALLS": str(calls),
                },
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            args = calls.read_text()
            self.assertIn("realms/online-beratung", args)
            self.assertIn('supportedLocales=["de","en","fr","ru","ti","tr"]', args)
            for field in (
                "internationalizationEnabled=true",
                "defaultLocale=de",
                "emailTheme=oriso",
            ):
                self.assertIn(field, args)
            self.assertNotIn("smtpServer", args)

    def test_helm_job_reconciles_existing_realms_on_install_and_upgrade(self):
        result = subprocess.run(
            [
                "helm", "template", "mail-locales", str(ROOT),
                "-f", str(ROOT / "values.yaml.default"),
                "-f", str(ROOT / "tests/fixtures/values-render-domain.yaml"),
                "-f", str(ROOT / "secrets.yaml.default"),
                "--set", "userService.smtpHost=",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        job = next(
            doc for doc in yaml.safe_load_all(result.stdout)
            if doc and doc.get("kind") == "Job"
            and doc["metadata"]["name"] == "keycloak-reconcile-mail-locales"
        )
        self.assertEqual(
            "post-install,post-upgrade", job["metadata"]["annotations"]["helm.sh/hook"]
        )
        container = job["spec"]["template"]["spec"]["containers"][0]
        self.assertIn(SCRIPT_PATH.read_text().strip(), container["command"][-1])
        env = {item["name"]: item for item in container["env"]}
        self.assertEqual(
            {"name": "keycloak-secret-env", "key": "KEYCLOAK_ADMIN_PASSWORD"},
            env["KEYCLOAK_ADMIN_PASSWORD"]["valueFrom"]["secretKeyRef"],
        )


if __name__ == "__main__":
    unittest.main()
