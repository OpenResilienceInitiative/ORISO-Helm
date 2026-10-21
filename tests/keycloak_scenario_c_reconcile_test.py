#!/usr/bin/env python3
"""Real pinned-Keycloak proof for standalone scenario C preparation.

Uses isolated realms; never changes live ORISO settings. Runtime activation is
synthetic fixture setup only, after the disabled-clients reconciliation proof.
"""

import base64
import ast
import importlib.util
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
        cls.cli_diagnostics = []
        cls.provisioner_observations = {}
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
            # Export only exact source-authored refusal literals and structural
            # mapper mismatches. Never retain native bodies, tokens or secrets.
            literals = {
                node.args[0].value
                for node in ast.walk(ast.parse(SCRIPT.read_text()))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ReconcileError"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            }
            reason = next(
                (
                    text
                    for text in literals
                    if result.stderr.strip()
                    == "Scenario C preparation refused: " + text
                ),
                "UNCLASSIFIED_REFUSAL",
            )
            spec = importlib.util.spec_from_file_location(
                "installer_diagnostic", SCRIPT
            )
            installer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(installer)
            registry = installer.registry(
                ROOT / "files/scenario-c-task-identities.json"
            )
            observation = {"reason": reason, "mapperMismatches": []}
            for client in self.admin("GET", "/clients"):
                if client["clientId"] not in {
                    "backend-setup",
                    "backend-account-create",
                    "backend-account-admin",
                    "backend-mail",
                    "backend-runtime",
                }:
                    continue
                actor = next(
                    actor
                    for actor in registry.values()
                    if actor["clientId"] == client["clientId"]
                )
                desired = {row["name"]: row for row in installer.token_mappers(actor)}
                actual = self.admin(
                    "GET", "/clients/" + client["id"] + "/protocol-mappers/models"
                )
                for mapper in actual:
                    if mapper.get("name") not in desired:
                        observation["mapperMismatches"].append(
                            {"actor": actor["key"], "unknownMapper": True}
                        )
                        continue
                    expected = desired[mapper["name"]]
                    config = mapper.get("config", {})
                    differing = [
                        key
                        for key in expected["config"]
                        if config.get(key) != expected["config"][key]
                    ]
                    extra = sorted(set(config) - set(expected["config"]))
                    # Config field names are not emitted unless part of the finite
                    # source-authored public mapper key set.
                    public = {key for row in desired.values() for key in row["config"]}
                    if (
                        differing
                        or extra
                        or mapper.get("protocolMapper") != expected["protocolMapper"]
                    ):
                        observation["mapperMismatches"].append(
                            {
                                "actor": actor["key"],
                                "mapper": mapper["name"],
                                "differentExpectedFields": sorted(differing),
                                "extraKnownFields": sorted(set(extra) & public),
                                "extraUnknownFieldCount": len(set(extra) - public),
                                "mapperTypeMatches": mapper.get("protocolMapper")
                                == expected["protocolMapper"],
                            }
                        )
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
            type(self).cli_diagnostics.append(observation)
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
        # The actual upgrade helper must inspect retired clients without
        # re-enabling, rotating or requiring any of their credentials.
        retired = {}
        for client_id in ("backend-technical", "backend-admin", "smtp-sync"):
            client = self.admin("GET", "/clients?clientId=" + client_id)[0]
            self.admin("PUT", "/clients/" + client["id"], {"enabled": False})
            retired[client["id"]] = (
                self.admin("GET", "/clients/" + client["id"]),
                self.admin("GET", "/clients/" + client["id"] + "/client-secret"),
            )
        env = {"KEYCLOAK_URL": self.container.base, "KEYCLOAK_REALM": self.realm,
               "POD_NAMESPACE": "fixture", "KEYCLOAK_ADMIN_USERNAME": shared.INSTALLER[0],
               "KEYCLOAK_ADMIN_PASSWORD": shared.INSTALLER[1],
               "TECHNICAL_CLIENT_ID": "backend-technical", "ADMIN_CLIENT_ID": "backend-admin",
               "HUMAN_CLIENT_ID": "app", "SMTP_SYNC_CLIENT_ID": "smtp-sync",
               "TASK_IDENTITY_MODEL": "FIVE_ACTORS"}
        self.assertIn("LEGACY_SERVICE_CLIENTS_INSPECTED", shared.run_script(
            "keycloak-reconcile-service-clients.py", env))
        for cid, before in retired.items():
            after = (self.admin("GET", "/clients/" + cid),
                     self.admin("GET", "/clients/" + cid + "/client-secret"))
            self.assertTrue(before == after, "Inspect-only hook changed a retired client")

    def test_five_prepared_actors_issue_exact_native_subject_role_audience_tokens(self):
        self.cli("--apply", "--confirm-isolated-realm", "--receivers-only")
        self.assertEqual(
            0,
            self.cli("--apply", "--confirm-isolated-realm", "--receivers-only")[
                "changes"
            ],
        )
        registry = json.loads(
            (ROOT / "files/scenario-c-task-identities.json").read_text()
        )
        for actor in registry["actors"]:
            with self.subTest(actor=actor["key"]):
                # Enabling and setting synthetic fixture credentials happen only
                # after disabled preparation has independently converged twice.
                token = self.actor_token(actor["clientId"].removeprefix("backend-"))
                payload = token.split(".")[1]
                claims = json.loads(
                    base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
                )
                client = next(
                    c
                    for c in self.admin("GET", "/clients")
                    if c["clientId"] == actor["clientId"]
                )
                user = self.admin(
                    "GET", "/clients/" + client["id"] + "/service-account-user"
                )
                self.assertEqual(
                    self.container.base + "/realms/" + self.realm, claims.get("iss")
                )
                self.assertEqual(actor["clientId"], claims.get("azp"))
                self.assertEqual(user["id"], claims.get("sub"))
                self.assertEqual(
                    set(actor["roles"]),
                    set(claims.get("realm_access", {}).get("roles", [])),
                )
                audiences = claims.get("aud", [])
                if isinstance(audiences, str):
                    audiences = [audiences]
                self.assertEqual(set(actor["bearerAudiences"]), set(audiences))
                self.assertEqual(len(actor["bearerAudiences"]), len(audiences))
                self.assertFalse(claims.get("resource_access"))

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
        creator_status = self.native(
            creator,
            "POST",
            "/users",
            {
                "username": "owned-pending",
                "enabled": False,
                "groups": ["/pending-provisioning"],
            },
        )[0]
        maintainer_status = self.native(
            maintainer,
            "POST",
            "/users",
            {
                "username": "maintainer-pending",
                "enabled": False,
                "groups": ["/pending-provisioning"],
            },
        )[0]
        type(self).provisioner_observations = {
            "creatorPendingCreateStatus": creator_status,
            "maintainerPendingCreateStatus": maintainer_status,
        }
        self.assertEqual(201, creator_status)
        self.assertEqual(403, maintainer_status)
        self.assertEqual(
            [], self.admin("GET", "/users?username=maintainer-pending&exact=true")
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

    def test_receiver_only_policies_refuse_stock_mutation_with_real_actor_tokens(self):
        self.prepare()
        converted = self.cli("--apply", "--confirm-isolated-realm", "--receivers-only")
        self.assertEqual("RECEIVER_ONLY", converted["policy_model"])
        self.assertEqual(
            0,
            self.cli("--apply", "--confirm-isolated-realm", "--receivers-only")[
                "changes"
            ],
        )
        ordinary = self.user("receiver-only-human", ["/human-roles/consultant"])
        pending = self.user("receiver-only-pending", ["/pending-provisioning"])
        protected = self.user(
            "receiver-only-protected", ["/protected-admins/platform-admins"]
        )
        self.admin("PUT", "/users/" + pending, {"enabled": False})
        group = self.admin("GET", "/group-by-path/human-roles/consultant")["id"]
        before = {
            uid: self.admin("GET", "/users/" + uid)
            for uid in (ordinary, pending, protected)
        }
        for actor in ("account-create", "account-admin"):
            token = self.actor_token(actor)
            for uid in (ordinary, pending, protected):
                for method, suffix, body in (
                    (
                        "PUT",
                        "",
                        {
                            "enabled": True,
                            "attributes": {"attempt-owner": ["rewritten"]},
                        },
                    ),
                    (
                        "PUT",
                        "/reset-password",
                        {
                            "type": "password",
                            "value": "synthetic-policy-only-credential",
                            "temporary": False,
                        },
                    ),
                    ("PUT", "/groups/" + group, None),
                    ("DELETE", "", None),
                ):
                    with self.subTest(
                        actor=actor, method=method, target=uid, operation=suffix
                    ):
                        self.assertEqual(
                            403,
                            self.native(token, method, "/users/" + uid + suffix, body)[
                                0
                            ],
                        )
            self.assertEqual(
                403,
                self.native(
                    token,
                    "POST",
                    "/users",
                    {
                        "username": "forbidden-" + actor,
                        "enabled": False,
                        "groups": ["pending-provisioning"],
                    },
                )[0],
            )
        after = {uid: self.admin("GET", "/users/" + uid) for uid in before}
        self.assertEqual(before, after)
        self.assertFalse(after[pending]["enabled"])

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
