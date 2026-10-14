#!/usr/bin/env python3
"""Standalone scenario C reconciliation at its CLI and Admin HTTP seams."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/reconcile-keycloak-scenario-c.py"
MAPPING = {
    "version": 1,
    "groups": [
        {"path": "/human-roles/user", "roles": ["user"]},
        {
            "path": "/human-roles/consultant",
            "roles": ["consultant", "group-chat-consultant"],
        },
        {
            "path": "/human-roles/agency-admin",
            "roles": ["restricted-agency-admin", "user-admin"],
        },
        {
            "path": "/protected-admins/platform-admins",
            "roles": ["tenant-admin", "agency-admin", "user-admin", "topic-admin"],
        },
    ],
}


class NativeRealm:
    """Scripted external API; readback state is queried through HTTP in tests."""

    def __init__(self):
        self.enabled = False
        self.clients = {
            "realm-management": {"id": "management", "clientId": "realm-management"}
        }
        self.users = {}
        self.policies = {}
        self.roles = {
            r: {"id": r, "name": r} for g in MAPPING["groups"] for r in g["roles"]
        }
        self.groups = {
            g["path"]: {"id": "group-" + str(n), "path": g["path"], "roles": g["roles"]}
            for n, g in enumerate(MAPPING["groups"])
        }
        for n, path in enumerate(
            (
                "/human-roles",
                "/protected-admins",
                "/protected-admins/technical-identities",
                "/pending-provisioning",
            ),
            10,
        ):
            self.groups[path] = {"id": "group-" + str(n), "path": path, "roles": []}
        self.writes = 0
        self.readback_fault = False
        self.default_role = None
        self.ignore_role_write = False

    def handle(self, method, path, body):
        url = urlparse(path)
        parts = [unquote(p) for p in url.path.split("/")[4:]]
        q = parse_qs(url.query)
        if method != "GET":
            self.writes += 1
        if not parts or parts == [""]:
            if method == "PUT":
                self.enabled = body["adminPermissionsEnabled"]
                if self.enabled:
                    self.clients["admin-permissions"] = {
                        "id": "permissions",
                        "clientId": "admin-permissions",
                    }
            return 200, {
                "realm": "fixture",
                "adminPermissionsEnabled": self.enabled,
                "defaultRole": {"name": self.default_role} if self.default_role else {},
            }
        if parts == ["clients"]:
            if method == "POST":
                cid = body["clientId"]
                self.clients[cid] = {**body, "id": cid + "-uuid"}
                self.users[cid] = {
                    "id": cid + "-subject",
                    "username": "service-account-" + cid,
                    "serviceAccountClientId": cid,
                    "realmRoles": [self.default_role] if self.default_role else [],
                    "clientRoles": [],
                    "groups": [],
                }
                return 201, None
            rows = list(self.clients.values())
            if "clientId" in q:
                rows = [c for c in rows if c["clientId"] == q["clientId"][0]]
            return 200, rows
        if parts == ["users"]:
            return 200, [
                u
                for u in self.users.values()
                if u["username"] == q.get("username", [None])[0]
            ]
        if parts[:1] == ["roles"]:
            if method == "POST":
                self.roles[body["name"]] = {"id": body["name"], **body}
                return 201, None
            return (
                (200, self.roles[parts[1]]) if parts[1] in self.roles else (404, None)
            )
        if parts[:1] == ["group-by-path"]:
            g = self.groups.get("/" + "/".join(parts[1:]))
            return (200, g) if g else (404, None)
        if parts[:1] == ["groups"]:
            g = next(g for g in self.groups.values() if g["id"] == parts[1])
            if parts[2:] == ["role-mappings", "realm", "composite"]:
                return 200, [self.roles[r] for r in g["roles"]]
            if parts[2:] == ["role-mappings"]:
                return 200, {
                    "realmMappings": [self.roles[r] for r in g["roles"]],
                    "clientMappings": {},
                }
            if parts[2:] == ["children"]:
                return 200, [
                    child
                    for child in self.groups.values()
                    if child["path"].rsplit("/", 1)[0] == g["path"]
                ]
        if parts[:1] == ["users"] and len(parts) > 1:
            u = next(u for u in self.users.values() if u["id"] == parts[1])
            tail = parts[2:]
            if not tail:
                return 200, {
                    k: v
                    for k, v in u.items()
                    if k not in ("realmRoles", "clientRoles", "groups")
                }
            if tail == ["role-mappings", "realm", "composite"] or tail == [
                "role-mappings",
                "realm",
            ]:
                if method == "DELETE":
                    u["realmRoles"] = [
                        r for r in u["realmRoles"] if r not in {x["name"] for x in body}
                    ]
                    return 204, None
                if method == "POST":
                    if not self.ignore_role_write:
                        u["realmRoles"] = list(
                            {*u["realmRoles"], *(r["name"] for r in body)}
                        )
                    return 204, None
                return 200, [self.roles[r] for r in u["realmRoles"]]
            if tail == ["role-mappings"]:
                return 200, {
                    "realmMappings": [self.roles[r] for r in u["realmRoles"]],
                    "clientMappings": (
                        {"management": {"mappings": u["clientRoles"]}}
                        if u["clientRoles"]
                        else {}
                    ),
                }
            if tail == ["groups"]:
                return 200, [g for g in self.groups.values() if g["id"] in u["groups"]]
            if tail[:1] == ["groups"] and method == "PUT":
                u["groups"].append(tail[1])
                return 204, None
        if parts[:1] == ["clients"] and len(parts) > 2:
            c = next(c for c in self.clients.values() if c["id"] == parts[1])
            if parts[2:] == ["service-account-user"]:
                return 200, self.users[c["clientId"]]
            if parts[2:4] == ["authz", "resource-server"]:
                tail = parts[4:]
                if method == "POST":
                    pid = body["name"]
                    self.policies[pid] = {"id": pid, "type": tail[1], **body}
                    return 201, self.policies[pid]
                if method == "PUT":
                    self.policies[tail[2]] = {"id": tail[2], "type": tail[1], **body}
                    return 204, None
                if len(tail) == 1:
                    return 200, [
                        p
                        for p in self.policies.values()
                        if tail[0] != "permission" or p["type"] in ("scope", "resource")
                    ]
                pid = tail[2]
                item = self.policies[pid]
                if len(tail) == 3:
                    value = dict(item)
                    if self.readback_fault:
                        value["users"] = ["foreign-subject"]
                    return 200, value
                field = tail[3]
                if field == "resources":
                    return 200, [
                        {"_id": "authz-" + x, "name": x} for x in item.get(field, [])
                    ]
                if field == "scopes":
                    return 200, [{"name": x} for x in item.get(field, [])]
                if field == "associatedPolicies":
                    return 200, [{"id": x} for x in item.get("policies", [])]
        if parts[:1] == ["clients"] and len(parts) == 2:
            c = next(c for c in self.clients.values() if c["id"] == parts[1])
            if method == "PUT":
                c.update(body)
                return 204, None
            return 200, c
        return 404, None


class HttpReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.realm = NativeRealm()
        realm = self.realm

        class Handler(BaseHTTPRequestHandler):
            def do_request(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                status, data = realm.handle(
                    self.command, self.path, json.loads(raw) if raw else None
                )
                self.send_response(status)
                self.end_headers()
                if data is not None:
                    self.wfile.write(json.dumps(data).encode())

            do_GET = do_POST = do_PUT = do_DELETE = do_request

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.mapping = Path(self.temp.name) / "mapping.json"
        self.mapping.write_text(json.dumps(MAPPING))

    def run_cli(self, *args):
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--url",
                "http://127.0.0.1:" + str(self.server.server_port),
                "--realm",
                "fixture",
                "--mapping",
                str(self.mapping),
                *args,
            ],
            env={
                **os.environ,
                "ORISO_KEYCLOAK_INSTALLER_TOKEN": "synthetic-private-installer-token",
            },
            capture_output=True,
            text=True,
        )

    def test_default_plan_creates_nothing_and_keeps_runtime_disabled(self):
        result = self.run_cli()
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(5, report["actor_count"])
        self.assertFalse(report["applied"])
        self.assertFalse(report["runtime_cutover"])
        self.assertEqual(0, self.realm.writes)
        self.assertFalse(self.realm.enabled)
        self.assertNotIn(
            "synthetic-private-installer-token", result.stdout + result.stderr
        )

    def test_apply_reconciles_five_disabled_actors_and_native_policies_twice(self):
        first = self.run_cli("--apply", "--confirm-isolated-realm")
        self.assertEqual(0, first.returncode, first.stderr)
        report = json.loads(first.stdout)
        self.assertTrue(report["applied"])
        self.assertGreater(report["changes"], 0)
        self.assertEqual(7, report["native_policy_count"])
        self.assertEqual(5, len(self.realm.users))
        self.assertTrue(
            all(
                c["enabled"] is False
                for c in self.realm.clients.values()
                if c["clientId"].startswith("backend-")
            )
        )
        writes = self.realm.writes
        second = self.run_cli("--apply", "--confirm-isolated-realm")
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(0, json.loads(second.stdout)["changes"])
        self.assertEqual(writes, self.realm.writes)

    def test_composite_actor_role_is_refused_before_any_write(self):
        self.realm.roles["smtp-sync"] = {
            "id": "smtp-sync",
            "name": "smtp-sync",
            "composite": True,
        }
        result = self.run_cli("--apply", "--confirm-isolated-realm")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(0, self.realm.writes)

    def test_apply_without_isolated_copy_confirmation_is_read_only(self):
        result = self.run_cli("--apply")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(0, self.realm.writes)

    def test_foreign_all_users_permission_is_refused_without_writes(self):
        self.realm.enabled = True
        self.realm.clients["admin-permissions"] = {
            "id": "permissions",
            "clientId": "admin-permissions",
        }
        self.realm.policies["broad"] = {
            "id": "broad",
            "name": "foreign-all-users",
            "type": "scope",
            "resourceType": "Users",
            "resources": [],
        }
        result = self.run_cli("--apply", "--confirm-isolated-realm")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(0, self.realm.writes)

    def test_native_readback_mismatch_does_not_report_success(self):
        self.realm.readback_fault = True
        result = self.run_cli("--apply", "--confirm-isolated-realm")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("readback", result.stderr)

    def test_undeclared_groups_roles_and_client_shortcuts_are_refused(self):
        first = self.run_cli("--apply", "--confirm-isolated-realm")
        self.assertEqual(0, first.returncode, first.stderr)
        self.realm.users["backend-mail"]["clientRoles"] = [
            {"id": "manage-realm", "name": "manage-realm"}
        ]
        writes = self.realm.writes
        result = self.run_cli("--apply", "--confirm-isolated-realm")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(writes, self.realm.writes)

    def test_reviewed_combined_ordinary_agency_roles_are_preserved(self):
        result = self.run_cli("--apply", "--confirm-isolated-realm")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            ["restricted-agency-admin", "user-admin"],
            self.realm.groups["/human-roles/agency-admin"]["roles"],
        )

    def test_imported_default_role_name_is_removed_from_actors_only(self):
        self.realm.default_role = "default-roles-original-export"
        self.realm.roles[self.realm.default_role] = {
            "id": "old-default",
            "name": self.realm.default_role,
            "composite": True,
        }
        result = self.run_cli("--apply", "--confirm-isolated-realm")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(
            all(
                self.realm.default_role not in u["realmRoles"]
                for u in self.realm.users.values()
            )
        )

    def test_unknown_human_group_child_cannot_inherit_native_grants(self):
        self.realm.groups["/human-roles/unreviewed"] = {
            "id": "unexpected-child",
            "path": "/human-roles/unreviewed",
            "roles": ["tenant-admin"],
        }
        result = self.run_cli("--apply", "--confirm-isolated-realm")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(0, self.realm.writes)

    def test_acknowledged_but_missing_role_grants_do_not_report_convergence(self):
        self.realm.ignore_role_write = True
        result = self.run_cli("--apply", "--confirm-isolated-realm")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("grants", result.stderr)


class RegistryContractTest(unittest.TestCase):
    def test_registry_has_exactly_five_selected_actors_and_purpose_aliases(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--validate-only"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(
            {
                "SETUP": "backend-setup",
                "ACCOUNT_CREATE": "backend-account-create",
                "ACCOUNT_ADMIN": "backend-account-admin",
                "MAIL": "backend-mail",
                "RUNTIME": "backend-runtime",
            },
            report["clients"],
        )
        self.assertEqual("SETUP", report["purposes"]["CONSULTANT_IMPORT"])
        self.assertEqual("ACCOUNT_ADMIN", report["purposes"]["OTP"])
        self.assertEqual("MAIL", report["purposes"]["SMTP_SYNC"])
        self.assertEqual("RUNTIME", report["purposes"]["APPOINTMENT_CLEANUP"])
        self.assertFalse(report["runtime_cutover"])


if __name__ == "__main__":
    unittest.main()
