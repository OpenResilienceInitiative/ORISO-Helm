"""Check the existing UserService notification retention controls through Helm."""

import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
READ_DAYS = "EVENT_NOTIFICATION_RETENTION_READ_DAYS"
ABSOLUTE_DAYS = "EVENT_NOTIFICATION_RETENTION_ABSOLUTE_DAYS"


class UserServiceNotificationRetentionTest(unittest.TestCase):
    def assert_retention_wiring(self, expected_read, expected_absolute, *extra_args):
        command = ["helm", "template", "caritas", str(ROOT), "--namespace", "caritas"]
        for values in (
            "values.yaml.default",
            "secrets.yaml.default",
            "tests/fixtures/values-render-domain.yaml",
            "tests/fixtures/render-required-secrets.yaml",
            "values-dev.yaml",
        ):
            command.extend(["-f", str(ROOT / values)])
        command.extend([
            "--set-string", "global.emailLegalOrganisationName=Render Test Foundation",
            "--show-only", "templates/userservice/userservice-configmap-env.yaml",
            "--show-only", "templates/userservice/userservice-deployment.yaml",
            *extra_args,
        ])
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        documents = {
            (doc["kind"], doc["metadata"]["name"]): doc
            for doc in yaml.safe_load_all(result.stdout)
            if doc
        }
        config = documents["ConfigMap", "userservice-configmap-env"]
        deployment = documents["Deployment", "userservice"]
        containers = deployment["spec"]["template"]["spec"]["containers"]
        userservice = next(item for item in containers if item["name"] == "userservice")
        for name, expected in ((READ_DAYS, expected_read), (ABSOLUTE_DAYS, expected_absolute)):
            with self.subTest(name=name):
                self.assertEqual(config["data"].get(name), expected)
                self.assertEqual([env for env in userservice["env"] if env["name"] == name], [{
                    "name": name,
                    "valueFrom": {"configMapKeyRef": {
                        "name": config["metadata"]["name"],
                        "key": name,
                    }},
                }])

    def test_dev_defaults_match_the_application(self):
        self.assert_retention_wiring("90", "365")

    def test_an_existing_installation_can_omit_the_new_values(self):
        self.assert_retention_wiring("90", "365", "--set", "userService.eventNotificationRetention=null")

    def test_a_positive_override_does_not_change_the_other_period(self):
        self.assert_retention_wiring("120", "365", "--set", "userService.eventNotificationRetention.readDays=120")

    def test_unset_individual_values_use_the_application_defaults(self):
        self.assert_retention_wiring(
            "90", "365",
            "--set", "userService.eventNotificationRetention.readDays=null",
            "--set-string", "userService.eventNotificationRetention.absoluteDays=",
        )

    def test_explicit_nonpositive_values_disable_either_cutoff(self):
        self.assert_retention_wiring(
            "0", "-7",
            "--set", "userService.eventNotificationRetention.readDays=0",
            "--set", "userService.eventNotificationRetention.absoluteDays=-7",
        )
