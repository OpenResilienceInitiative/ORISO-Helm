#!/usr/bin/env python3
"""Migration CLI behavior at the public Keycloak Admin HTTP boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/migrate-keycloak-human-role-groups.py"


class Realm:
    """Small external Admin API fixture, with role effects observable via HTTP."""

    def __init__(self):
        self.roles = {
            n: {"id": n, "name": n}
            for n in (
                "user",
                "consultant",
                "user-admin",
                "restricted-agency-admin",
                "tenant-admin",
                "agency-admin",
                "single-tenant-admin",
                "topic-admin",
                "offline_access",
                "uma_authorization",
                "technical",
                "unexpected",
            )
        }
        self.groups = {}
        self.users = {}
        self.writes = []
        self.fault = None
        self.clients = [{"id": "account-uuid", "clientId": "account"}]

    def user(self, name, roles, **extra):
        if extra.get("serviceAccountClientId") and not any(
            c["clientId"] == extra["serviceAccountClientId"] for c in self.clients
        ):
            self.clients.append(
                {
                    "id": extra["serviceAccountClientId"] + "-uuid",
                    "clientId": extra["serviceAccountClientId"],
                    "serviceAccountsEnabled": True,
                }
            )
        self.users[name] = {"id": name, "direct": set(roles), "groups": set(), **extra}

    def effective(self, user):
        result = set(user["direct"])
        for path in user["groups"]:
            for ancestor, group in self.groups.items():
                if path == ancestor or path.startswith(ancestor + "/"):
                    result.update(group["roles"])
        return result

    def role_list(self, names):
        return [self.roles[n] for n in sorted(names)]

    def handle(self, method, path, data):
        url = urlparse(path)
        parts = unquote(url.path).split("/")[4:]
        query = parse_qs(url.query)
        if method != "GET":
            self.writes.append((method, url.path))
        if parts == ["clients"]:
            return 200, self.clients
        if (
            len(parts) == 3
            and parts[0] == "clients"
            and parts[2] == "service-account-user"
        ):
            # Native Keycloak may lazily create the account even though this endpoint is GET.
            if "lazy-service" not in self.users:
                self.user("lazy-service", [], serviceAccountClientId="lazy-client")
            return 200, {"id": "lazy-service"}
        if parts == ["users"]:
            first = int(query.get("first", ["0"])[0])
            maximum = int(query.get("max", ["100"])[0])
            return 200, [
                {k: v for k, v in u.items() if k not in ("direct", "groups")}
                for u in [
                    v
                    for v in self.users.values()
                    if not v.get("serviceAccountClientId") or "exact" in query
                ][first : first + maximum]
            ]
        if parts[0] == "roles":
            return (
                (200, self.roles[parts[1]]) if parts[1] in self.roles else (404, None)
            )
        if parts[0] == "group-by-path":
            target = "/" + "/".join(parts[1:])
            group = self.groups.get(target)
            return (200, {"id": group["id"], "path": target}) if group else (404, None)
        if parts == ["groups"] and method == "POST":
            target = "/" + data["name"]
            self.groups[target] = {"id": target, "roles": set()}
            return 201, None
        if parts[0] == "groups":
            # Group IDs contain slashes only in fixture: encoded paths are decoded after splitting.
            rest = url.path.split("/groups/", 1)[1].split("/")
            gid = unquote(rest[0])
            tail = rest[1:]
            group = self.groups[gid]
            if tail == ["children"] and method == "POST":
                target = gid + "/" + data["name"]
                self.groups[target] = {"id": target, "roles": set()}
                return 201, None
            if tail == ["role-mappings", "realm"]:
                if method == "GET":
                    return 200, self.role_list(group["roles"])
                group["roles"].update(r["name"] for r in data)
                return 204, None
            if tail == ["role-mappings", "realm", "composite"]:
                return 200, self.role_list(group["roles"])
            if tail[:2] == ["role-mappings", "clients"]:
                return 200, []
        if parts[0] == "users":
            uid = parts[1]
            user = self.users[uid]
            tail = parts[2:]
            if tail == ["groups"]:
                return 200, [{"id": p, "path": p} for p in sorted(user["groups"])]
            if tail[:1] == ["groups"]:
                gid = unquote(url.path.split("/groups/", 1)[1])
                if method == "PUT":
                    if self.fault != "ignore-join":
                        user["groups"].add(gid)
                else:
                    user["groups"].discard(gid)
                return 204, None
            if tail == ["role-mappings"]:
                return 200, {
                    "realmMappings": self.role_list(user["direct"]),
                    "clientMappings": {
                        client: {
                            "id": client,
                            "mappings": [{"id": name, "name": name} for name in roles],
                        }
                        for client, roles in user.get("clientRoles", {}).items()
                    },
                }
            if tail == ["role-mappings", "realm", "composite"]:
                return 200, self.role_list(self.effective(user))
            if tail == ["role-mappings", "realm"]:
                if method == "GET":
                    return 200, self.role_list(user["direct"])
                if method == "DELETE":
                    user["direct"].difference_update(r["name"] for r in data)
                    if self.fault in ("delete-response-lost", "restore-unavailable"):
                        return 500, None
                else:
                    if self.fault == "restore-unavailable":
                        return 500, None
                    user["direct"].update(r["name"] for r in data)
                return 204, None
            if tail[:2] == ["role-mappings", "clients"]:
                return 200, [
                    {"id": n, "name": n}
                    for n in user.get("clientRoles", {}).get(tail[2], [])
                ]
        raise AssertionError((method, url.path, parts, data))


class MigrationCliTest(unittest.TestCase):
    def setUp(self):
        self.realm = Realm()
        realm = self.realm

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def serve(self):
                data = (
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    if int(self.headers.get("Content-Length", "0"))
                    else None
                )
                try:
                    status, body = realm.handle(self.command, self.path, data)
                except Exception as error:
                    status, body = 500, {"error": str(error)}
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if body is not None:
                    self.wfile.write(json.dumps(body).encode())

            do_GET = do_POST = do_PUT = do_DELETE = serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.mapping = Path(self.temp.name) / "map.json"
        self.mapping.write_text(
            json.dumps(
                {
                    "version": 1,
                    "groups": [
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
                    ],
                }
            )
        )

    def run_cli(self, apply=False):
        command = [
            "python3",
            str(SCRIPT),
            "--url",
            f"http://127.0.0.1:{self.server.server_port}",
            "--realm",
            "copy",
            "--mapping",
            str(self.mapping),
        ]
        if apply:
            command.append("--apply")
        env = dict(os.environ, ORISO_KEYCLOAK_ADMIN_TOKEN="synthetic-test-only-token")
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertNotIn("synthetic-test-only-token", result.stdout + result.stderr)
        self.assertTrue(result.stdout.strip(), result.stderr)
        return result.returncode, json.loads(result.stdout)

    def api(self, path):
        with urlopen(
            f"http://127.0.0.1:{self.server.server_port}/admin/realms/copy" + path
        ) as response:
            return json.load(response)

    def test_apply_preserves_roles_with_one_group_and_second_run_is_unchanged(self):
        self.realm.user(
            "existing-consultant", ["consultant", "offline_access", "uma_authorization"]
        )
        before = self.api("/users/existing-consultant/role-mappings/realm/composite")
        code, report = self.run_cli(apply=True)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["users"][0]["status"], "migrated")
        self.assertEqual(
            self.api("/users/existing-consultant/role-mappings/realm/composite"), before
        )
        self.assertEqual(
            self.api("/users/existing-consultant/groups"),
            [{"id": "/human-roles/consultant", "path": "/human-roles/consultant"}],
        )
        self.assertEqual(
            self.api("/users/existing-consultant/role-mappings/realm"),
            [
                {"id": "offline_access", "name": "offline_access"},
                {"id": "uma_authorization", "name": "uma_authorization"},
            ],
        )
        self.realm.writes.clear()
        code, report = self.run_cli(apply=True)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["users"][0]["status"], "unchanged")
        self.assertEqual(self.realm.writes, [])

    def test_unknown_combinations_and_service_accounts_are_reported_unchanged(self):
        self.realm.user("odd-human", ["consultant", "user"])
        self.realm.user(
            "svc", ["consultant"], serviceAccountClientId="synthetic-service"
        )
        self.realm.user("technical-legacy", ["technical"])
        code, report = self.run_cli(apply=True)
        self.assertEqual(code, 3, report)
        self.assertEqual(
            {u["id"]: u["status"] for u in report["users"]},
            {
                "odd-human": "unclassifiable",
                "technical-legacy": "unclassifiable",
            },
        )
        self.assertEqual(
            report["technicalClients"],
            [
                {
                    "id": "synthetic-service-uuid",
                    "clientId": "synthetic-service",
                    "status": "preserved",
                }
            ],
        )
        self.assertEqual(self.realm.writes, [])
        self.assertEqual(
            self.api("/users/svc/role-mappings/realm"),
            [{"id": "consultant", "name": "consultant"}],
        )

    def test_combined_centre_admin_and_protected_platform_admin_keep_exact_roles(self):
        self.realm.user("centre-admin", ["restricted-agency-admin", "user-admin"])
        self.realm.user(
            "platform-admin", ["tenant-admin", "agency-admin", "user-admin"]
        )
        code, report = self.run_cli(apply=True)
        self.assertEqual(code, 0, report)
        self.assertEqual(
            [u["status"] for u in report["users"]], ["migrated", "migrated"]
        )
        self.assertEqual(
            [g["path"] for g in self.api("/users/centre-admin/groups")],
            ["/human-roles/agency-admin"],
        )
        self.assertEqual(
            [g["path"] for g in self.api("/users/platform-admin/groups")],
            ["/protected-admins/platform-admins"],
        )
        self.assertEqual(
            [
                r["name"]
                for r in self.api("/users/centre-admin/role-mappings/realm/composite")
            ],
            ["restricted-agency-admin", "user-admin"],
        )
        self.assertEqual(
            [
                r["name"]
                for r in self.api("/users/platform-admin/role-mappings/realm/composite")
            ],
            ["agency-admin", "tenant-admin", "user-admin"],
        )

    def test_dry_run_reports_group_overgrant_before_any_user_changes(self):
        self.realm.user("existing-consultant", ["consultant"])
        self.realm.groups["/human-roles"] = {
            "id": "/human-roles",
            "roles": {"unexpected"},
        }
        self.realm.groups["/human-roles/consultant"] = {
            "id": "/human-roles/consultant",
            "roles": {"consultant"},
        }
        code, report = self.run_cli()
        self.assertEqual(code, 2, report)
        self.assertEqual(report["users"][0]["status"], "failed")
        self.assertEqual(self.realm.writes, [])
        self.assertEqual(self.api("/users/existing-consultant/groups"), [])

    def test_admin_roles_cannot_be_mapped_to_an_ordinary_human_group(self):
        self.realm.user("platform-admin", ["tenant-admin"])
        self.mapping.write_text(
            json.dumps(
                {
                    "version": 1,
                    "groups": [
                        {"path": "/human-roles/tenant-admin", "roles": ["tenant-admin"]}
                    ],
                }
            )
        )
        code, report = self.run_cli(apply=True)
        self.assertEqual(code, 2, report)
        self.assertIn("protected", report["error"])
        self.assertEqual(self.realm.writes, [])

    def test_join_without_membership_readback_never_removes_direct_roles(self):
        self.realm.user("existing-consultant", ["consultant"])
        self.realm.fault = "ignore-join"
        code, report = self.run_cli(apply=True)
        self.assertEqual(code, 2, report)
        self.assertIn("original effective roles restored", report["users"][0]["error"])
        self.assertEqual(
            self.api("/users/existing-consultant/role-mappings/realm"),
            [{"id": "consultant", "name": "consultant"}],
        )
        self.assertEqual(self.api("/users/existing-consultant/groups"), [])

    def test_direct_client_roles_require_manual_classification(self):
        self.realm.user(
            "client-admin-human",
            ["consultant"],
            clientRoles={"account-uuid": ["manage-users"]},
        )
        code, report = self.run_cli(apply=True)
        self.assertEqual(code, 3, report)
        self.assertEqual(report["users"][0]["status"], "unclassifiable")
        self.assertEqual(report["users"][0]["reason"], "direct-client-role-mappings")
        self.assertEqual(self.realm.writes, [])

    def test_lost_delete_response_restores_direct_grants_before_removing_membership(
        self,
    ):
        self.realm.user("existing-consultant", ["consultant"])
        self.realm.fault = "delete-response-lost"
        code, report = self.run_cli(apply=True)
        self.assertEqual(code, 2, report)
        self.assertIn("original effective roles restored", report["users"][0]["error"])
        self.assertEqual(
            self.api("/users/existing-consultant/role-mappings/realm"),
            [{"id": "consultant", "name": "consultant"}],
        )
        self.assertEqual(self.api("/users/existing-consultant/groups"), [])

    def test_fresh_realm_is_safe_and_conflicting_groups_are_not_reclassified(self):
        code, report = self.run_cli(apply=True)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["users"], [])
        self.assertEqual(self.realm.writes, [])
        self.realm.groups.update(
            {
                "/human-roles": {"id": "/human-roles", "roles": set()},
                "/human-roles/user": {"id": "/human-roles/user", "roles": {"user"}},
                "/human-roles/consultant": {
                    "id": "/human-roles/consultant",
                    "roles": set(),
                },
            }
        )
        self.realm.user("conflicting-human", ["user"])
        self.realm.users["conflicting-human"]["groups"] = {
            "/human-roles/user",
            "/human-roles/consultant",
        }
        code, report = self.run_cli(apply=True)
        self.assertEqual(code, 2, report)
        self.assertEqual(self.realm.writes, [])

    def test_explicit_protected_variants_preserve_distinct_existing_role_sets(self):
        self.realm.user(
            "platform-basic", ["tenant-admin", "agency-admin", "user-admin"]
        )
        self.realm.user(
            "platform-topic",
            ["tenant-admin", "agency-admin", "user-admin", "topic-admin"],
        )
        mapping = json.loads(self.mapping.read_text())
        mapping["groups"].append(
            {
                "path": "/protected-admins/platform-admins-with-topics",
                "roles": ["tenant-admin", "agency-admin", "user-admin", "topic-admin"],
            }
        )
        self.mapping.write_text(json.dumps(mapping))
        code, report = self.run_cli(apply=True)
        self.assertEqual(code, 0, report)
        self.assertEqual(
            [g["path"] for g in self.api("/users/platform-basic/groups")],
            ["/protected-admins/platform-admins"],
        )
        self.assertEqual(
            [g["path"] for g in self.api("/users/platform-topic/groups")],
            ["/protected-admins/platform-admins-with-topics"],
        )
        self.assertNotIn(
            "topic-admin",
            [
                r["name"]
                for r in self.api("/users/platform-basic/role-mappings/realm/composite")
            ],
        )

    def test_explicit_mapping_cannot_move_a_runtime_capability_into_human_roles(self):
        self.mapping.write_text(
            json.dumps(
                {
                    "version": 1,
                    "groups": [
                        {"path": "/human-roles/wizard", "roles": ["config-wizard"]}
                    ],
                }
            )
        )
        code, report = self.run_cli(apply=True)
        self.assertEqual(code, 2, report)
        self.assertIn("Technical", report["error"])
        self.assertEqual(self.realm.writes, [])

    def test_dry_run_does_not_materialize_a_missing_technical_service_account(self):
        self.realm.user("existing-human", ["consultant"])
        self.realm.clients.append(
            {
                "id": "lazy-client-uuid",
                "clientId": "lazy-client",
                "serviceAccountsEnabled": True,
            }
        )
        before = self.api("/users?exact=true")
        code, report = self.run_cli()
        self.assertEqual(code, 0, report)
        self.assertEqual(self.api("/users?exact=true"), before)
        self.assertEqual(self.realm.writes, [])

    def test_failed_restoration_keeps_last_group_grant_and_stops_further_users(self):
        self.realm.user("first-human", ["consultant"])
        self.realm.user("next-human", ["user"])
        self.realm.fault = "restore-unavailable"
        code, report = self.run_cli(apply=True)
        self.assertEqual(code, 2, report)
        self.assertIn("recovery incomplete", report["users"][0]["error"])
        self.assertEqual(report["scanned"], 1)
        self.assertEqual(report["total"], 2)
        self.assertEqual(
            self.api("/users/first-human/role-mappings/realm/composite"),
            [{"id": "consultant", "name": "consultant"}],
        )
        self.assertEqual(
            [g["path"] for g in self.api("/users/first-human/groups")],
            ["/human-roles/consultant"],
        )
        self.assertEqual(self.api("/users/next-human/groups"), [])
        self.assertEqual(
            self.api("/users/next-human/role-mappings/realm"),
            [{"id": "user", "name": "user"}],
        )

    def test_dry_run_classifies_existing_humans_without_writing(self):
        self.realm.user(
            "existing-consultant", ["consultant", "offline_access", "uma_authorization"]
        )
        code, report = self.run_cli()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["users"][0]["status"], "would-migrate")
        self.assertEqual(report["users"][0]["group"], "/human-roles/consultant")
        self.assertEqual(self.realm.writes, [])


if __name__ == "__main__":
    unittest.main()
