"""Render the actual Job/CronJob SMTP credential boundary in both identity modes."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml
from test_scenario_c_chart_bindings import bindings

ROOT = Path(
    os.environ.get("ORISO_SCENARIO_C_CHART_ROOT", Path(__file__).resolve().parents[1])
)


class ScenarioCSmtpChartTest(unittest.TestCase):
    def render(self, config):
        with tempfile.TemporaryDirectory() as private:
            values = Path(private) / "values.yaml"
            values.write_text(yaml.safe_dump({"global": {"taskIdentities": config}}))
            command = ["helm", "template", "mail-proof", str(ROOT)]
            for source in (
                "values.yaml.default",
                "tests/fixtures/values-render-domain.yaml",
                "secrets.yaml.default",
                "tests/fixtures/render-required-secrets.yaml",
            ):
                command += ["-f", str(ROOT / source)]
            command += [
                "-f",
                str(values),
                "--set-string",
                "global.secrets.redisdefaultPass=synthetic-render",
                "--set-string",
                "tenantService.smtpPasswordEncryptionSecret=synthetic-render",
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=40)
            self.assertEqual(0, result.returncode, result.stderr)
            docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
            rows = []
            for kind in ("Job", "CronJob"):
                doc = next(
                    doc
                    for doc in docs
                    if doc["kind"] == kind
                    and doc["metadata"]["name"] == "keycloak-reconcile-smtp"
                )
                template = (
                    doc["spec"]["template"]
                    if kind == "Job"
                    else doc["spec"]["jobTemplate"]["spec"]["template"]
                )
                env = template["spec"]["containers"][0]["env"]
                self.assertEqual(len(env), len({row["name"] for row in env}))
                rows.append({row["name"]: row for row in env})
            return rows

    def test_c_job_and_cron_mount_only_setup_and_common_context_secret(self):
        config = bindings()
        for env in self.render(config):
            self.assertIn("TASK_IDENTITY_MODEL", env)
            self.assertEqual("FIVE_ACTORS", env["TASK_IDENTITY_MODEL"]["value"])
            self.assertEqual(
                config["realmId"], env["KEYCLOAK_REALM_INTERNAL_ID"]["value"]
            )
            self.assertEqual("backend-setup", env["IDENTITY_SETUP_CLIENT_ID"]["value"])
            self.assertEqual(
                config["actors"]["SETUP"]["serviceSubject"],
                env["IDENTITY_SETUP_SERVICE_SUBJECT"]["value"],
            )
            self.assertEqual(
                config["actors"]["SETUP"]["secretRef"],
                env["IDENTITY_SETUP_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"],
            )
            self.assertEqual(
                config["contextKeySecretRef"],
                env["ORISO_TASK_CONTEXT_KEY"]["valueFrom"]["secretKeyRef"],
            )
            for name in (
                "TECHNICAL_CLIENT_ID",
                "TECHNICAL_CLIENT_SECRET",
                "TECHNICAL_SERVICE_SUBJECT",
                "SMTP_SYNC_CLIENT_ID",
                "SMTP_SYNC_CLIENT_SECRET",
            ):
                self.assertNotIn(name, env)

    def test_c_public_issuer_binding_is_distinct_from_internal_transport_url(self):
        for env in self.render(bindings()):
            self.assertIn("TASK_IDENTITY_ISSUER", env)
            self.assertTrue(env["TASK_IDENTITY_ISSUER"]["value"].startswith("https://"))
            self.assertNotEqual(
                env["TASK_IDENTITY_ISSUER"]["value"], env["KEYCLOAK_URL"]["value"]
            )

    def test_legacy_job_and_cron_keep_established_credentials_without_c_secret(self):
        for env in self.render({"model": "LEGACY"}):
            self.assertIn("TECHNICAL_CLIENT_SECRET", env)
            self.assertIn("SMTP_SYNC_CLIENT_SECRET", env)
            self.assertNotIn("IDENTITY_SETUP_CLIENT_SECRET", env)
            self.assertNotIn("ORISO_TASK_CONTEXT_KEY", env)


if __name__ == "__main__":
    unittest.main()
