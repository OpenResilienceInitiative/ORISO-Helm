#!/usr/bin/env python3
"""Real pinned-Keycloak proof for standalone scenario C preparation.

Uses isolated realms; never changes live ORISO settings. Runtime activation is
synthetic fixture setup only, after the disabled-clients reconciliation proof.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from urllib.parse import quote

import _keycloak_container as shared
from keycloak_native_admin_permissions_test import call
from test_reconcile_keycloak_scenario_c import MAPPING

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/reconcile-keycloak-scenario-c.py"


class NativeScenarioCReconcileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.container = shared.KeycloakContainer(
            "scenario-c-policies", os.environ.get("SCENARIO_C_KEYCLOAK_URL")
        )
        cls.container.start()

    @classmethod
    def tearDownClass(cls):
        cls.container.stop()

    def setUp(self):
        self.realm = "scenario-c-" + uuid.uuid4().hex[:8]
        realm = shared.strip_ids(shared.rendered_realm())
        realm["realm"] = self.realm
        realm["adminPermissionsEnabled"] = False
        realm["groups"] = [
            {"name": "pending-provisioning"},
            {
                "name": "human-roles",
                "subGroups": [
                    {"name": g["path"].rsplit("/", 1)[1], "realmRoles": g["roles"]}
                    for g in MAPPING["groups"]
                    if g["path"].startswith("/human-roles/")
                ],
            },
            {
                "name": "protected-admins",
                "subGroups": [
                    {"name": "technical-identities"},
                    *(
                        {"name": g["path"].rsplit("/", 1)[1], "realmRoles": g["roles"]}
                        for g in MAPPING["groups"]
                        if g["path"].startswith("/protected-admins/")
                    ),
                ],
            },
        ]
        shared.request(
            self.container.base,
            "POST",
            "/admin/realms",
            realm,
            token=shared.master_admin(self.container.base),
        )
        self.addCleanup(lambda: self.admin("DELETE", ""))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.mapping = Path(self.temp.name) / "groups.json"
        self.mapping.write_text(json.dumps(MAPPING))

    def admin(self, method, path, body=None):
        return shared.request(
            self.container.base,
            method,
            "/admin/realms/" + self.realm + path,
            body,
            token=shared.master_admin(self.container.base),
        )

    def client_snapshot(self):
        """Native scope arrays are sets; never retain secrets in assertions."""
        rows = []
        for client in self.admin("GET", "/clients"):
            value = {
                k: v
                for k, v in client.items()
                if k not in ("secret", "registrationAccessToken")
            }
            for field in (
                "defaultClientScopes",
                "optionalClientScopes",
                "redirectUris",
                "webOrigins",
            ):
                if field in value:
                    value[field] = sorted(value[field])
            if "protocolMappers" in value:
                value["protocolMappers"] = sorted(
                    value["protocolMappers"], key=lambda mapper: mapper["id"]
                )
            rows.append(value)
        return sorted(rows, key=lambda client: client["id"])

    def cli(self, *args):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--url",
                self.container.base,
                "--realm",
                self.realm,
                "--mapping",
                str(self.mapping),
                *args,
            ],
            env={
                **os.environ,
                "ORISO_KEYCLOAK_INSTALLER_TOKEN": shared.master_admin(
                    self.container.base
                ),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode:
            for client in self.admin("GET", "/clients"):
                if client["clientId"] not in {
                    "backend-setup",
                    "backend-account-create",
                    "backend-account-admin",
                    "backend-mail",
                    "backend-runtime",
                }:
                    continue
                users = self.admin(
                    "GET",
                    "/users?username=service-account-"
                    + client["clientId"]
                    + "&exact=true&briefRepresentation=false",
                )
                if not users:
                    continue
                grants = self.admin(
                    "GET", "/users/" + users[0]["id"] + "/role-mappings"
                )
                print(
                    "Synthetic actor grants:",
                    client["clientId"],
                    [r["name"] for r in grants.get("realmMappings", [])],
                    {
                        k: [r["name"] for r in v.get("mappings", [])]
                        for k, v in grants.get("clientMappings", {}).items()
                    },
                )
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def test_fresh_chart_realm_installer_setup_and_second_reconcile_converge(self):
        self.assertFalse(self.cli()["applied"])
        first = self.cli("--apply", "--confirm-isolated-realm")
        self.assertGreater(first["changes"], 0)
        self.assertEqual(7, first["native_policy_count"])
        self.assertEqual(0, self.cli("--apply", "--confirm-isolated-realm")["changes"])
        actors = [
            c
            for c in self.admin("GET", "/clients")
            if c["clientId"]
            in {
                "backend-setup",
                "backend-account-create",
                "backend-account-admin",
                "backend-mail",
                "backend-runtime",
            }
        ]
        self.assertEqual(5, len(actors))
        self.assertTrue(all(c["enabled"] is False for c in actors))
        self.assertTrue(self.admin("GET", "")["adminPermissionsEnabled"])

    def prepare(self):
        self.cli("--apply", "--confirm-isolated-realm")

    def actor_token(self, actor):
        cid = "backend-" + actor
        client = next(c for c in self.admin("GET", "/clients") if c["clientId"] == cid)
        secret = "synthetic-scenario-c-" + actor
        self.admin(
            "PUT", "/clients/" + client["id"], {"enabled": True, "secret": secret}
        )
        return shared.request(
            self.container.base,
            "POST",
            "/realms/" + self.realm + "/protocol/openid-connect/token",
            {
                "grant_type": "client_credentials",
                "client_id": cid,
                "client_secret": secret,
            },
            form=True,
        )["access_token"]

    def user(self, username, groups):
        self.admin(
            "POST",
            "/partialImport",
            {
                "ifResourceExists": "FAIL",
                "users": [{"username": username, "enabled": True, "groups": groups}],
            },
        )
        return self.admin("GET", "/users?username=" + username + "&exact=true")[0]["id"]

    def native(self, token, method, path, body=None):
        return call(
            self.container.base,
            method,
            "/admin/realms/" + self.realm + path,
            body,
            token,
        )

    def test_existing_humans_and_managed_dry_run_are_preserved(self):
        human = self.user("existing-centre-admin", ["/human-roles/agency-admin"])
        protected = self.user(
            "existing-platform-admin", ["/protected-admins/platform-admins"]
        )
        before = {uid: self.admin("GET", "/users/" + uid) for uid in (human, protected)}
        self.prepare()
        actors = self.client_snapshot()
        account_ids = [
            c["attributes"]["oriso-c.prepared-subject"]
            for c in actors
            if c["clientId"]
            in {
                "backend-setup",
                "backend-account-create",
                "backend-account-admin",
                "backend-mail",
                "backend-runtime",
            }
        ]
        user_count = self.admin("GET", "/users/count")
        dry = self.cli()
        self.assertFalse(dry["native_service_accounts_verified"])
        self.assertEqual(user_count, self.admin("GET", "/users/count"))
        self.assertEqual(actors, self.client_snapshot())
        self.assertEqual(0, self.cli("--apply", "--confirm-isolated-realm")["changes"])
        self.assertEqual(
            before, {uid: self.admin("GET", "/users/" + uid) for uid in before}
        )
        self.assertEqual(
            5, len({self.admin("GET", "/users/" + uid)["id"] for uid in account_ids})
        )

    def test_real_actor_tokens_keep_group_commit_and_protected_denials(self):
        self.prepare()
        human = self.user("existing-consultant", ["/human-roles/consultant"])
        centre_admin = self.user("existing-centre-admin", ["/human-roles/agency-admin"])
        protected = self.user(
            "existing-platform-admin", ["/protected-admins/platform-admins"]
        )
        creator, maintainer, mail = (
            self.actor_token(a) for a in ("account-create", "account-admin", "mail")
        )
        self.assertEqual(
            403,
            self.native(
                creator,
                "POST",
                "/users",
                {"username": "without-pending", "enabled": False},
            )[0],
        )
        self.assertEqual(
            201,
            self.native(
                creator,
                "POST",
                "/users",
                {
                    "username": "owned-pending",
                    "enabled": False,
                    "groups": ["/pending-provisioning"],
                },
            )[0],
        )
        pending = self.admin("GET", "/users?username=owned-pending&exact=true")[0]["id"]
        self.assertEqual(200, self.native(creator, "GET", "/users/" + pending)[0])
        self.assertFalse(self.native(creator, "GET", "/users/" + pending)[1]["enabled"])
        consultant_group = self.admin("GET", "/group-by-path/human-roles/consultant")[
            "id"
        ]
        self.assertEqual(
            204,
            self.native(
                creator, "PUT", "/users/" + pending + "/groups/" + consultant_group
            )[0],
        )
        self.assertEqual(403, self.native(creator, "DELETE", "/users/" + pending)[0])
        self.assertEqual(200, self.native(maintainer, "GET", "/users/" + human)[0])
        self.assertEqual(
            204,
            self.native(
                maintainer, "PUT", "/users/" + human, {"firstName": "Maintained"}
            )[0],
        )
        self.assertEqual(
            "Maintained",
            self.native(maintainer, "GET", "/users/" + human)[1]["firstName"],
        )
        self.assertEqual(
            403,
            self.native(creator, "PUT", "/users/" + human, {"firstName": "Forbidden"})[
                0
            ],
        )
        self.assertEqual(
            200, self.native(maintainer, "GET", "/users/" + centre_admin)[0]
        )
        for token in (creator, maintainer, mail):
            self.assertEqual(403, self.native(token, "GET", "/users/" + protected)[0])
            self.assertEqual(
                403,
                self.native(
                    token, "PUT", "/users/" + protected, {"firstName": "Forbidden"}
                )[0],
            )
            self.assertEqual(
                403, self.native(token, "PUT", "", {"registrationAllowed": True})[0]
            )
        self.assertEqual(403, self.native(mail, "GET", "/users/" + human)[0])
        self.assertEqual(
            403,
            self.native(
                mail, "PUT", "", {"smtpServer": {"host": "mail.example.invalid"}}
            )[0],
        )
        # A grouped MAIL actor has no native SMTP authority. #57 remains open.

    def test_unmanaged_existing_actor_is_refused_without_adoption(self):
        self.admin(
            "POST",
            "/clients",
            {
                "clientId": "backend-mail",
                "enabled": False,
                "publicClient": False,
                "serviceAccountsEnabled": True,
                "standardFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "fullScopeAllowed": False,
            },
        )
        before = self.client_snapshot()
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--url",
                self.container.base,
                "--realm",
                self.realm,
                "--mapping",
                str(self.mapping),
                "--apply",
                "--confirm-isolated-realm",
            ],
            env={
                **os.environ,
                "ORISO_KEYCLOAK_INSTALLER_TOKEN": shared.master_admin(
                    self.container.base
                ),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("adoption is not authorized", result.stderr)
        self.assertEqual(before, self.client_snapshot())
        self.assertFalse(self.admin("GET", "")["adminPermissionsEnabled"])


if __name__ == "__main__":
    unittest.main()
