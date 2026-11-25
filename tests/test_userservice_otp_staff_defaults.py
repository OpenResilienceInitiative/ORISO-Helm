"""Every new staff account must set up 2FA, so the OTP "allowed" flags for staff
roles must default to true.

"Allowed" only permits enrolment; it does not force it. While it is false,
UserService answers 2FA enrolment with 409 and never announces the requirement.
The advice-seeker template fallback stays "false". Staff flags use `dig`, not
`| default`, because `default` treats an explicit boolean false as empty and
would turn an operator's `--set ...=false` into "true".
"""

import re
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "userservice" / "userservice-configmap-env.yaml"
STAFF = {
    "IDENTITY_OTP_ALLOWED_FOR_CONSULTANTS": "otpAllowedForConsultants",
    "IDENTITY_OTP_ALLOWED_FOR_SINGLE_TENANT_ADMINS": "otpAllowedForSingleTenantAdmins",
    "IDENTITY_OTP_ALLOWED_FOR_TENANT_SUPER_ADMINS": "otpAllowedForTenantSuperAdmins",
    "IDENTITY_OTP_ALLOWED_FOR_RESTRICTED_AGENCY_ADMINS": "otpAllowedForRestrictedAgencyAdmins",
}


DEPLOYMENT = ROOT / "templates" / "userservice" / "userservice-deployment.yaml"


def staff_line(env_key):
    match = re.search(rf"^\s*{env_key}:(.*)$", TEMPLATE.read_text(), re.M)
    return match.group(1) if match else ""


def default_of(env_key):
    match = re.search(rf"^\s*{env_key}:.*\| default \"(\w+)\"", TEMPLATE.read_text(), re.M)
    return match.group(1) if match else None


class UserServiceOtpStaffDefaults(unittest.TestCase):
    def test_staff_flags_default_to_true_and_keep_an_explicit_false(self):
        for env_key, value_key in STAFF.items():
            line = staff_line(env_key)
            self.assertIn(f'dig "{value_key}" "true" .Values.userService', line, env_key)
            self.assertNotIn("| default", line, env_key)

    def test_pods_restart_when_the_env_configmap_changes(self):
        self.assertIn(
            'checksum/config-env: {{ include (print $.Template.BasePath '
            '"/userservice/userservice-configmap-env.yaml") . | sha256sum }}',
            DEPLOYMENT.read_text(),
        )

    def test_advice_seeker_template_fallback_is_false(self):
        self.assertEqual("false", default_of("IDENTITY_OTP_ALLOWED_FOR_USERS"))

    def test_dev_renders_tenant_super_admin_otp_and_wires_userservice(self):
        args = ["helm", "template", "caritas", str(ROOT), "--namespace", "caritas"]
        for values in (
            "values.yaml.default",
            "secrets.yaml.default",
            "tests/fixtures/values-render-domain.yaml",
            "tests/fixtures/render-required-secrets.yaml",
            "values-dev.yaml",
        ):
            args.extend(["-f", str(ROOT / values)])
        args.extend([
            "--set-string", "global.emailLegalOrganisationName=Render Test Foundation",
            "--show-only", "templates/userservice/userservice-configmap-env.yaml",
            "--show-only", "templates/userservice/userservice-deployment.yaml",
        ])
        rendered = subprocess.run(args, capture_output=True, text=True, check=False)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        resources = {
            (doc["kind"], doc["metadata"]["name"]): doc
            for doc in yaml.safe_load_all(rendered.stdout)
            if doc
        }

        key = "IDENTITY_OTP_ALLOWED_FOR_TENANT_SUPER_ADMINS"
        config_map = resources["ConfigMap", "userservice-configmap-env"]
        self.assertEqual(config_map["data"].get(key), "true")
        deployment = resources["Deployment", "userservice"]
        containers = deployment["spec"]["template"]["spec"]["containers"]
        userservice = next(container for container in containers if container["name"] == "userservice")
        entries = [entry for entry in userservice["env"] if entry["name"] == key]
        self.assertEqual(entries, [{
            "name": key,
            "valueFrom": {"configMapKeyRef": {"name": config_map["metadata"]["name"], "key": key}},
        }])


if __name__ == "__main__":
    unittest.main()
