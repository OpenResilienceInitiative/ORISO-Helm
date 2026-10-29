"""Every new staff account must set up 2FA, so the OTP "allowed" flags for staff
roles must default to true and Pre-Dev must set them explicitly.

"Allowed" only permits enrolment; it does not force it. While it is false,
UserService answers 2FA enrolment with 409 and never announces the requirement.
Advice seekers (otpAllowedForUsers) stay opt-in. Pure source checks, no helm.
"""

import re
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


def default_of(env_key):
    match = re.search(rf"^\s*{env_key}:.*\| default \"(\w+)\"", TEMPLATE.read_text(), re.M)
    return match.group(1) if match else None


class UserServiceOtpStaffDefaults(unittest.TestCase):
    def test_staff_flags_default_to_true(self):
        for env_key in STAFF:
            self.assertEqual("true", default_of(env_key), env_key)

    def test_advice_seeker_flag_stays_opt_in(self):
        self.assertEqual("false", default_of("IDENTITY_OTP_ALLOWED_FOR_USERS"))

    def test_pre_dev_sets_staff_flags_explicitly(self):
        values = yaml.safe_load((ROOT / "values-pre-dev.yaml").read_text())["userService"]
        for value_key in STAFF.values():
            self.assertEqual("true", values.get(value_key), value_key)


if __name__ == "__main__":
    unittest.main()
