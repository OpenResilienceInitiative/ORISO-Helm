#!/usr/bin/env python3
"""Opt-in migration against a real, isolated, digest-pinned Keycloak.

Exercises the CLI at the Admin REST seam; no live ORISO realm is used or changed.
Set GROUP_MIGRATION_KEYCLOAK_URL to reuse an isolated synthetic fixture only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
from urllib.parse import quote, urlencode

import _keycloak_container as shared

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/migrate-keycloak-human-role-groups.py"
GROUPS = [
    {"path": "/human-roles/user", "roles": ["user"]},
    {"path": "/human-roles/consultant", "roles": ["consultant"]},
    {
        "path": "/human-roles/agency-admin",
        "roles": ["restricted-agency-admin", "user-admin"],
    },
    {
        "path": "/protected-admins/platform-admins",
        "roles": ["tenant-admin", "agency-admin", "user-admin"],
    },
    {
        "path": "/protected-admins/traeger-admins",
        "roles": ["single-tenant-admin", "restricted-agency-admin"],
    },
]


class RealHumanRoleMigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.container = shared.KeycloakContainer(
            "human-role-migration", os.environ.get("GROUP_MIGRATION_KEYCLOAK_URL")
        )
        cls.container.start()

    @classmethod
    def tearDownClass(cls):
        cls.container.stop()

    def setUp(self):
        self.realm = "migration-" + uuid.uuid4().hex[:8]
        roles = sorted(
            {r for g in GROUPS for r in g["roles"]}
            | {"technical", "unclassified-role", "group-chat-consultant", "topic-admin"}
        )
        shared.request(
            self.container.base,
            "POST",
            "/admin/realms",
            {
                "realm": self.realm,
                "enabled": True,
                "roles": {"realm": [{"name": r} for r in roles]},
            },
            token=shared.master_admin(self.container.base),
        )
        self.addCleanup(self.drop_realm)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.mapping = Path(self.temp.name) / "mapping.json"
        self.mapping.write_text(json.dumps({"version": 1, "groups": GROUPS}))

    def drop_realm(self):
        shared.request(
            self.container.base,
            "DELETE",
            "/admin/realms/" + self.realm,
            token=shared.master_admin(self.container.base),
        )

    def admin(self, method, path, body=None):
        return shared.request(
            self.container.base,
            method,
            "/admin/realms/" + self.realm + path,
            body,
            token=shared.master_admin(self.container.base),
        )

    def seed(self, name, roles):
        self.admin(
            "POST",
            "/partialImport",
            {
                "ifResourceExists": "FAIL",
                "users": [{"username": name, "enabled": True, "realmRoles": roles}],
            },
        )
        return self.admin(
            "GET", "/users?" + urlencode({"username": name, "exact": "true"})
        )[0]["id"]

    def client(self, client_id):
        return self.admin("GET", "/clients?" + urlencode({"clientId": client_id}))[0][
            "id"
        ]

    def names(self, path):
        return sorted(r["name"] for r in self.admin("GET", path))

    def run_cli(self, apply=False):
        command = [
            "python3",
            str(SCRIPT),
            "--url",
            self.container.base,
            "--realm",
            self.realm,
            "--mapping",
            str(self.mapping),
        ]
        if apply:
            command.append("--apply")
        token = shared.master_admin(self.container.base)
        result = subprocess.run(
            command,
            env=dict(os.environ, ORISO_KEYCLOAK_ADMIN_TOKEN=token),
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertNotIn(token, result.stdout + result.stderr)
        self.assertTrue(result.stdout.strip(), result.stderr)
        return result.returncode, json.loads(result.stdout)

    def test_fresh_existing_and_twice_preserve_complete_roles_and_protect_admins(self):
        code, report = self.run_cli(apply=True)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["users"], [])
        kind_users = {
            g["path"]: self.seed("kind-" + str(i), g["roles"])
            for i, g in enumerate(GROUPS)
        }
        account = self.client("account")
        before = {
            uid: (
                self.names("/users/" + uid + "/role-mappings/realm/composite"),
                self.names(
                    "/users/" + uid + "/role-mappings/clients/" + account + "/composite"
                ),
            )
            for uid in kind_users.values()
        }
        code, report = self.run_cli()
        self.assertEqual(code, 0, report)
        self.assertEqual([u["status"] for u in report["users"]], ["would-migrate"] * 5)
        for uid in kind_users.values():
            self.assertEqual(self.admin("GET", "/users/" + uid + "/groups"), [])
        code, report = self.run_cli(apply=True)
        self.assertEqual(code, 0, report)
        self.assertEqual([u["status"] for u in report["users"]], ["migrated"] * 5)
        for path, uid in kind_users.items():
            self.assertEqual(
                [g["path"] for g in self.admin("GET", "/users/" + uid + "/groups")],
                [path],
            )
            self.assertEqual(
                (
                    self.names("/users/" + uid + "/role-mappings/realm/composite"),
                    self.names(
                        "/users/"
                        + uid
                        + "/role-mappings/clients/"
                        + account
                        + "/composite"
                    ),
                ),
                before[uid],
            )
            self.assertFalse(
                set(self.names("/users/" + uid + "/role-mappings/realm"))
                & {r for g in GROUPS for r in g["roles"]}
            )
        code, report = self.run_cli(apply=True)
        self.assertEqual(code, 0, report)
        self.assertEqual([u["status"] for u in report["users"]], ["unchanged"] * 5)
        self.assertFalse(self.admin("GET", "").get("adminPermissionsEnabled", False))

    def test_reviewed_multi_role_variants_do_not_gain_existing_roles(self):
        variants = [
            {
                "path": "/human-roles/consultant-with-group-chat",
                "roles": ["consultant", "group-chat-consultant"],
            },
            {
                "path": "/human-roles/consultant-agency-admin",
                "roles": [
                    "consultant",
                    "group-chat-consultant",
                    "restricted-agency-admin",
                    "user-admin",
                ],
            },
            {
                "path": "/protected-admins/platform-admins-with-topics",
                "roles": ["tenant-admin", "agency-admin", "user-admin", "topic-admin"],
            },
        ]
        self.mapping.write_text(json.dumps({"version": 1, "groups": GROUPS + variants}))
        users = {
            g["path"]: self.seed("variant-" + str(i), g["roles"])
            for i, g in enumerate(variants)
        }
        before = {
            uid: self.names("/users/" + uid + "/role-mappings/realm/composite")
            for uid in users.values()
        }
        code, report = self.run_cli(apply=True)
        self.assertEqual(code, 0, report)
        for path, uid in users.items():
            self.assertEqual(
                [g["path"] for g in self.admin("GET", "/users/" + uid + "/groups")],
                [path],
            )
            self.assertEqual(
                self.names("/users/" + uid + "/role-mappings/realm/composite"),
                before[uid],
            )
        code, report = self.run_cli(apply=True)
        self.assertEqual(code, 0, report)
        self.assertEqual([u["status"] for u in report["users"]], ["unchanged"] * 3)

    def test_service_accounts_and_unknown_humans_are_not_changed(self):
        unknown = self.seed("unknown-human", ["consultant", "unclassified-role"])
        self.admin(
            "POST",
            "/clients",
            {
                "clientId": "synthetic-legacy",
                "protocol": "openid-connect",
                "publicClient": False,
                "serviceAccountsEnabled": True,
                "enabled": True,
            },
        )
        service = self.admin(
            "GET",
            "/clients/" + self.client("synthetic-legacy") + "/service-account-user",
        )["id"]
        role = self.admin("GET", "/roles/consultant")
        self.admin("POST", "/users/" + service + "/role-mappings/realm", [role])
        before = {
            uid: self.names("/users/" + uid + "/role-mappings/realm/composite")
            for uid in (service, unknown)
        }
        code, report = self.run_cli(apply=True)
        self.assertEqual(code, 3, report)
        self.assertEqual(
            {u["id"]: u["status"] for u in report["users"]},
            {unknown: "unclassifiable"},
        )
        self.assertEqual(
            report["technicalClients"],
            [
                {
                    "id": self.client("synthetic-legacy"),
                    "clientId": "synthetic-legacy",
                    "status": "preserved",
                }
            ],
        )
        for uid in (service, unknown):
            self.assertEqual(
                self.names("/users/" + uid + "/role-mappings/realm/composite"),
                before[uid],
            )
            self.assertEqual(self.admin("GET", "/users/" + uid + "/groups"), [])

    def test_existing_group_overgrant_blocks_migration_before_direct_roles_are_removed(
        self,
    ):
        uid = self.seed("existing-human", ["consultant"])
        self.admin(
            "POST",
            "/groups",
            {"name": "human-roles", "realmRoles": ["unclassified-role"]},
        )
        parent = self.admin("GET", "/group-by-path/human-roles")["id"]
        self.admin(
            "POST",
            "/groups/" + parent + "/children",
            {"name": "consultant", "realmRoles": ["consultant"]},
        )
        before = self.names("/users/" + uid + "/role-mappings/realm/composite")
        code, report = self.run_cli(apply=True)
        self.assertEqual(code, 2, report)
        self.assertEqual(report["users"][0]["status"], "failed")
        self.assertEqual(
            self.names("/users/" + uid + "/role-mappings/realm/composite"), before
        )
        self.assertEqual(self.admin("GET", "/users/" + uid + "/groups"), [])
        self.assertIn(
            "consultant", self.names("/users/" + uid + "/role-mappings/realm")
        )


if __name__ == "__main__":
    unittest.main()
