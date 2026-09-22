"""Behaviour of files/keycloak-reconcile-service-identities.sh.in against a fake kcadm.

The fake answers role lookups from a small JSON state file and records every call,
so these tests show what the script would do to a live realm without one.
"""

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "files" / "keycloak-reconcile-service-identities.sh.in"

FAKE_KCADM = r"""#!/usr/bin/env python3
import json, os, sys
state_path = os.environ["FAKE_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")

def arg(flag):
    return args[args.index(flag) + 1] if flag in args else None

cmd = args[0]
if cmd == "get" and args[1].startswith("roles/"):
    sys.exit(0 if args[1][len("roles/"):] in state["realm_roles"] else 1)
if cmd == "delete" and args[1].startswith("roles/"):
    state["realm_roles"].remove(args[1][len("roles/"):])
elif cmd == "create" and args[1] == "roles":
    state["realm_roles"].append(next(a[5:] for a in args if a.startswith("name=")))
elif cmd == "get" and args[1] == "users":
    wanted = next(a[len("username="):] for a in args if a.startswith("username="))
    if wanted in state["users"]:
        print(state["users"][wanted]["id"])
elif cmd == "create" and args[1] == "users":
    name = next(a[len("username="):] for a in args if a.startswith("username="))
    state["users"][name] = {"id": "id-" + name, "realm": [], "management": []}
elif cmd in ("add-roles", "remove-roles", "get-roles"):
    user = state["users"][arg("--uusername")]
    bucket = "management" if arg("--cclientid") == "realm-management" else "realm"
    role = arg("--rolename")
    if cmd == "get-roles":
        if os.environ.get("FAKE_FAIL_GET_ROLES"):
            sys.stderr.write("simulated get-roles failure\n")
            sys.exit(1)
        print("\n".join(user[bucket]))
    elif cmd == "add-roles" and role not in user[bucket]:
        user[bucket].append(role)
    elif cmd == "remove-roles" and role in user[bucket]:
        user[bucket].remove(role)
json.dump(state, open(state_path, "w"))
"""


class ReconcileServiceIdentitiesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.kcadm = tmp / "kcadm.sh"
        self.kcadm.write_text(FAKE_KCADM)
        self.kcadm.chmod(self.kcadm.stat().st_mode | stat.S_IEXEC)
        self.state = tmp / "state.json"
        self.log = tmp / "calls.log"
        self.log.write_text("")

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, state, **env):
        self.state.write_text(json.dumps(state))
        environment = {
            "PATH": os.environ["PATH"],
            "KCADM": str(self.kcadm),
            "FAKE_STATE": str(self.state),
            "FAKE_LOG": str(self.log),
            "KEYCLOAK_REALM": "online-beratung",
            **env,
        }
        result = subprocess.run(
            ["sh", str(SCRIPT)], env=environment, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(self.state.read_text()), result.stdout

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    ADMIN = {"SERVICE_ADMIN_USERNAME": "svc-keycloak-admin", "SERVICE_ADMIN_PASSWORD": "from-secret",
             "TECHNICAL_USERNAME": "technical", "TECHNICAL_SERVICE_SUBJECT": "t",
             "BOOTSTRAP_ADMIN_USERNAME": "realmadmin"}

    def run_failing(self, state, **env):
        self.state.write_text(json.dumps(state))
        result = subprocess.run(
            ["sh", str(SCRIPT)], capture_output=True, text=True,
            env={"PATH": os.environ["PATH"], "KCADM": str(self.kcadm),
                 "FAKE_STATE": str(self.state), "FAKE_LOG": str(self.log),
                 "KEYCLOAK_REALM": "online-beratung", **env},
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def mutating_calls(self):
        return [call for call in self.calls()
                if call[0] in ("add-roles", "remove-roles", "set-password", "create", "update", "delete")]

    @staticmethod
    def dev_like_realm():
        # What an existing environment looks like before stage 2.
        return {
            "realm_roles": ["technical", "tenant-admin", "TECHNICAL_DEFAULT"],
            "users": {"technical": {
                "id": "t",
                "realm": ["default-roles-online-beratung", "technical", "tenant-admin", "TECHNICAL_DEFAULT"],
                "management": ["manage-users", "view-users", "query-users"],
            }},
        }

    def test_creates_the_role_and_the_service_admin_with_exact_roles(self):
        state, _ = self.run_script(self.dev_like_realm(), **self.ADMIN)
        self.assertIn("otp-config-admin", state["realm_roles"])
        admin = state["users"]["svc-keycloak-admin"]
        self.assertEqual(
            sorted(admin["realm"]), ["default-roles-online-beratung", "otp-config-admin"]
        )
        self.assertEqual(
            sorted(admin["management"]),
            ["manage-users", "query-users", "view-realm", "view-users"],
        )
        self.assertIn(
            ["set-password", "-r", "online-beratung", "--username", "svc-keycloak-admin",
             "--new-password", "from-secret", "--temporary=false"],
            self.calls(),
        )

    def test_reduces_technical_to_a_pure_service_identity(self):
        state, _ = self.run_script(self.dev_like_realm(), **self.ADMIN)
        technical = state["users"]["technical"]
        self.assertEqual(sorted(technical["realm"]), ["default-roles-online-beratung", "technical"])
        self.assertEqual(technical["management"], [])
        # Its password is not touched here; keycloak-bootstrap-users owns it.
        self.assertNotIn("technical", [call[4] for call in self.calls() if call[0] == "set-password"])

    def test_removes_the_unused_technical_default_role(self):
        state, _ = self.run_script(self.dev_like_realm(), **self.ADMIN)
        self.assertNotIn("TECHNICAL_DEFAULT", state["realm_roles"])
        self.assertIn("tenant-admin", state["realm_roles"], "other realm roles stay")

    def test_strips_roles_that_were_added_by_hand(self):
        realm = self.dev_like_realm()
        realm["realm_roles"].append("otp-config-admin")
        realm["users"]["svc-keycloak-admin"] = {
            "id": "abc",
            "realm": ["tenant-admin", "technical", "otp-config-admin"],
            "management": ["realm-admin", "manage-users"],
        }
        state, _ = self.run_script(realm, **self.ADMIN)
        admin = state["users"]["svc-keycloak-admin"]
        self.assertNotIn("tenant-admin", admin["realm"])
        self.assertNotIn("technical", admin["realm"])
        self.assertNotIn("realm-admin", admin["management"])
        # An existing role is not recreated.
        self.assertNotIn(["create", "roles"], [call[:2] for call in self.calls()])

    def test_is_idempotent(self):
        first, _ = self.run_script(self.dev_like_realm(), **self.ADMIN)
        second, _ = self.run_script(first, **self.ADMIN)
        self.assertEqual(first, second)

    def test_a_missing_identity_fails_instead_of_skipping(self):
        for missing in ("SERVICE_ADMIN_USERNAME", "SERVICE_ADMIN_PASSWORD", "TECHNICAL_USERNAME",
                        "TECHNICAL_SERVICE_SUBJECT", "BOOTSTRAP_ADMIN_USERNAME"):
            env = {key: value for key, value in self.ADMIN.items() if key != missing}
            self.state.write_text(json.dumps(self.dev_like_realm()))
            result = subprocess.run(
                ["sh", str(SCRIPT)], capture_output=True, text=True,
                env={"PATH": os.environ["PATH"], "KCADM": str(self.kcadm),
                     "FAKE_STATE": str(self.state), "FAKE_LOG": str(self.log),
                     "KEYCLOAK_REALM": "online-beratung", **env},
            )
            self.assertNotEqual(result.returncode, 0, missing)
            self.assertIn(missing, result.stderr)

    def test_a_matching_technical_subject_passes(self):
        state, out = self.run_script(self.dev_like_realm(), **self.ADMIN)
        self.assertIn("technical subject matches", out)

    def test_a_wrong_subject_fails_before_any_change(self):
        env = {**self.ADMIN, "TECHNICAL_SERVICE_SUBJECT": "not-the-id"}
        result = self.run_failing(self.dev_like_realm(), **env)
        self.assertIn("serviceTechUserId", result.stderr)
        self.assertEqual(self.mutating_calls(), [])
        self.assertEqual(json.loads(self.state.read_text()), self.dev_like_realm())

    def test_the_admin_identity_may_not_be_the_service_identity(self):
        for name in ("technical", "TECHNICAL"):
            self.log.write_text("")
            result = self.run_failing(self.dev_like_realm(), **{**self.ADMIN, "SERVICE_ADMIN_USERNAME": name})
            self.assertIn("SERVICE_ADMIN_USERNAME", result.stderr)
            self.assertEqual(self.mutating_calls(), [], name)

    def test_the_admin_identity_may_not_be_the_bootstrap_admin(self):
        for env in ({"SERVICE_ADMIN_USERNAME": "realmadmin"},
                    {"SERVICE_ADMIN_USERNAME": "ops-admin", "BOOTSTRAP_ADMIN_USERNAME": "ops-admin"}):
            self.log.write_text("")
            result = self.run_failing(self.dev_like_realm(), **{**self.ADMIN, **env})
            self.assertIn("SERVICE_ADMIN_USERNAME", result.stderr)
            self.assertEqual(self.mutating_calls(), [], env)

    def test_a_failing_role_listing_fails_the_job(self):
        realm = self.dev_like_realm()
        result = self.run_failing(realm, FAKE_FAIL_GET_ROLES="1", **self.ADMIN)
        self.assertIn("could not list", result.stderr)
        # technical kept its roles: nothing was removed on a blind listing
        self.assertNotIn("remove-roles", [call[0] for call in self.calls()])

    def test_the_password_never_reaches_stdout(self):
        _, out = self.run_script(self.dev_like_realm(), **self.ADMIN)
        self.assertNotIn("from-secret", out)


if __name__ == "__main__":
    unittest.main()
