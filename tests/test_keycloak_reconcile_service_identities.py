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
if cmd == "create" and args[1] == "roles":
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

    def test_creates_the_role_and_the_service_admin_with_exact_roles(self):
        state, _ = self.run_script(
            {"realm_roles": ["technical"], "users": {}},
            SERVICE_ADMIN_USERNAME="svc-keycloak-admin",
            SERVICE_ADMIN_PASSWORD="from-secret",
        )
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

    def test_strips_roles_that_were_added_by_hand(self):
        state, _ = self.run_script(
            {
                "realm_roles": ["otp-config-admin", "technical", "tenant-admin"],
                "users": {
                    "svc-keycloak-admin": {
                        "id": "abc",
                        "realm": ["tenant-admin", "technical", "otp-config-admin"],
                        "management": ["realm-admin", "manage-users"],
                    }
                },
            },
            SERVICE_ADMIN_USERNAME="svc-keycloak-admin",
            SERVICE_ADMIN_PASSWORD="from-secret",
        )
        admin = state["users"]["svc-keycloak-admin"]
        self.assertNotIn("tenant-admin", admin["realm"])
        self.assertNotIn("technical", admin["realm"])
        self.assertNotIn("realm-admin", admin["management"])
        # An existing role is not recreated.
        self.assertNotIn(["create", "roles"], [call[:2] for call in self.calls()])

    def test_is_idempotent(self):
        first, _ = self.run_script(
            {"realm_roles": [], "users": {}},
            SERVICE_ADMIN_USERNAME="svc-keycloak-admin",
            SERVICE_ADMIN_PASSWORD="from-secret",
        )
        second, _ = self.run_script(
            first,
            SERVICE_ADMIN_USERNAME="svc-keycloak-admin",
            SERVICE_ADMIN_PASSWORD="from-secret",
        )
        self.assertEqual(first, second)

    def test_without_a_configured_identity_only_the_role_is_ensured(self):
        state, out = self.run_script({"realm_roles": [], "users": {"technical": {
            "id": "t", "realm": ["technical", "tenant-admin"], "management": ["manage-users"]}}})
        self.assertIn("SKIPPED", out)
        self.assertEqual(state["realm_roles"], ["otp-config-admin"])
        # Additive stage: the technical user is left exactly as it was.
        self.assertEqual(state["users"]["technical"]["realm"], ["technical", "tenant-admin"])
        self.assertEqual(state["users"]["technical"]["management"], ["manage-users"])

    def test_the_password_never_reaches_stdout(self):
        _, out = self.run_script(
            {"realm_roles": [], "users": {}},
            SERVICE_ADMIN_USERNAME="svc-keycloak-admin",
            SERVICE_ADMIN_PASSWORD="from-secret",
        )
        self.assertNotIn("from-secret", out)


if __name__ == "__main__":
    unittest.main()
