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
        self.native_service_accounts = {}
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
        self.requests = 0
        self.service_account_reads = 0
        self.lazy_service_account_creations = 0
        self.readback_fault = False
        self.default_role = None
        self.ignore_role_write = False
        self.native_initial_scopes = False
        self.fail_after_client_post = False
        self.fail_before_subject_receipt = False
        self.fail_after_subject_receipt = False
        self.drift_on_native_subject_read = False
        self.native_secret_timestamp = False

    def represented_client(self, client):
        value = json.loads(json.dumps(client))
        if self.native_secret_timestamp:
            value.setdefault('attributes', {})['realm_client'] = 'false'
        return value

    def handle(self, method, path, body):
        self.requests += 1
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
                "id": "fixture-realm-id",
                "realm": "fixture",
                "adminPermissionsEnabled": self.enabled,
                "defaultRole": {"name": self.default_role} if self.default_role else {},
            }
        if parts in (
            ["default-default-client-scopes"],
            ["default-optional-client-scopes"],
        ):
            names = (
                ["roles", "profile"]
                if parts[0] == "default-default-client-scopes"
                else ["offline_access"]
            )
            return 200, [{"id": name, "name": name} for name in names]
        if parts == ["client-scopes"]:
            return 200, [
                {
                    "id": "service_account",
                    "name": "service_account",
                    "protocol": "openid-connect",
                }
            ]
        if parts == ["clients"]:
            if method == "POST":
                cid = body["clientId"]
                self.clients[cid] = {**body, "id": cid + "-uuid"}
                if self.native_initial_scopes:
                    self.clients[cid]["defaultClientScopes"] = [
                        "roles",
                        "profile",
                        "service_account",
                    ]
                    self.clients[cid]["optionalClientScopes"] = ["offline_access"]
                self.users[cid] = {
                    "id": cid + "-subject",
                    "username": "service-account-" + cid,
                    "serviceAccountClientId": cid,
                    "realmRoles": [self.default_role] if self.default_role else [],
                    "clientRoles": [],
                    "groups": [],
                }
                self.native_service_accounts[cid] = self.users[cid]["id"]
                if self.native_secret_timestamp:
                    self.clients[cid]['attributes']['client.secret.creation.time'] = '1792616400'
                    self.clients[cid]['attributes']['backchannel.logout.session.required'] = 'true'
                    self.clients[cid]['attributes']['backchannel.logout.revoke.offline.tokens'] = 'false'
                    self.clients[cid]['secret'] = 'synthetic-auto-generated-client-credential'
                if self.fail_after_client_post:
                    self.fail_after_client_post = False
                    return 503, None
                return 201, None
            rows = list(self.clients.values())
            if "clientId" in q:
                rows = [c for c in rows if c["clientId"] == q["clientId"][0]]
            return 200, [self.represented_client(client) for client in rows]
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
            tail = parts[2:]
            if tail == ["protocol-mappers", "models"]:
                if method == "POST":
                    stored = {"id": body["name"], **body}
                    # Pinned RepresentationToModel.toModel removes null and
                    # empty map entries before native protocol mapper storage.
                    stored["config"] = {
                        key: value
                        for key, value in body.get("config", {}).items()
                        if value is not None and value != ""
                    }
                    c.setdefault("protocolMappers", []).append(stored)
                    return 201, None
                values = json.loads(json.dumps(c.get("protocolMappers", [])))
                # Pinned AbstractOIDCProtocolMapper.getEffectiveModel returns
                # explicit UserInfo/introspection defaults through this API.
                for value in values:
                    config = value.get("config", {})
                    if "id.token.claim" in config:
                        config.setdefault(
                            "userinfo.token.claim", config["id.token.claim"]
                        )
                    if "access.token.claim" in config:
                        config.setdefault(
                            "introspection.token.claim", config["access.token.claim"]
                        )
                return 200, values
            if tail[:1] in (["default-client-scopes"], ["optional-client-scopes"]):
                field = (
                    "defaultClientScopes"
                    if tail[0].startswith("default")
                    else "optionalClientScopes"
                )
                if method == "DELETE":
                    c[field].remove(tail[1])
                    return 204, None
                return 200, [{"id": name, "name": name} for name in c.get(field, [])]
            if tail == ["scope-mappings"]:
                return 200, {
                    "realmMappings": [self.roles[r] for r in c.get("scopeRoles", [])],
                    "clientMappings": c.get("scopeClientMappings", {}),
                }
            if tail == ["scope-mappings", "realm"]:
                if method == "POST":
                    c["scopeRoles"] = sorted(
                        {*c.get("scopeRoles", []), *(r["name"] for r in body)}
                    )
                    return 204, None
                return 200, [self.roles[r] for r in c.get("scopeRoles", [])]
            if parts[2:] == ["service-account-user"]:
                self.service_account_reads += 1
                if self.drift_on_native_subject_read:
                    c['enabled'] = True
                cid = c["clientId"]
                if cid not in self.native_service_accounts:
                    # Native Keycloak can create/link an account through this GET.
                    self.writes += 1
                    self.lazy_service_account_creations += 1
                    self.native_service_accounts[cid] = self.users[cid]["id"]
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
                if (self.fail_before_subject_receipt
                        and body.get('attributes', {}).get('oriso-c.prepared-subject')):
                    self.fail_before_subject_receipt = False
                    return 503, None
                # Pinned native client PUT merges attributes; explicit null
                # removes a key. Omission never clears a persisted attribute.
                fields = dict(body)
                attributes = fields.pop('attributes', None)
                c.update(fields)
                if attributes is not None:
                    for name, value in attributes.items():
                        if value is None:
                            c.setdefault('attributes', {}).pop(name, None)
                        else:
                            c.setdefault('attributes', {})[name] = value
                if (self.fail_after_subject_receipt
                        and body.get('attributes', {}).get('oriso-c.prepared-subject')):
                    self.fail_after_subject_receipt = False
                    return 503, None
                return 204, None
            return 200, self.represented_client(c)
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

    def pending_after_failure(self, fault='fail_after_client_post'):
        setattr(self.realm, fault, True)
        failed = self.run_cli('--apply', '--confirm-isolated-realm')
        self.assertNotEqual(0, failed.returncode)
        client = self.realm.clients['backend-setup']
        self.assertIn('oriso-c.pending-receipt', client.get('attributes', {}))
        return client, self.realm.users['backend-setup']['id']

    def test_lost_client_post_reply_resumes_owned_pending_without_lazy_dry_run(self):
        client, subject = self.pending_after_failure()
        writes, reads = self.realm.writes, self.realm.service_account_reads
        plan = self.run_cli()
        self.assertEqual(0, plan.returncode, plan.stderr)
        self.assertEqual((writes, reads), (self.realm.writes, self.realm.service_account_reads))
        resumed = self.run_cli('--apply', '--confirm-isolated-realm')
        self.assertEqual(0, resumed.returncode, resumed.stderr)
        self.assertEqual(5, len(self.realm.users))
        self.assertIs(client, self.realm.clients['backend-setup'])
        self.assertEqual(subject, client['attributes']['oriso-c.prepared-subject'])
        self.assertNotIn('oriso-c.pending-receipt', client['attributes'])

    def test_native_creation_timestamp_and_credential_survive_owned_pending_finalization(self):
        self.realm.native_secret_timestamp = True
        client, subject = self.pending_after_failure()
        stamp, secret = client['attributes']['client.secret.creation.time'], client['secret']
        plan = self.run_cli()
        self.assertEqual(0, plan.returncode, plan.stderr)
        resumed = self.run_cli('--apply', '--confirm-isolated-realm')
        self.assertEqual(0, resumed.returncode, resumed.stderr)
        self.assertEqual(subject, client['attributes']['oriso-c.prepared-subject'])
        self.assertEqual(stamp, client['attributes']['client.secret.creation.time'])
        self.assertTrue(secret == client['secret'], 'Pending recovery rotated native credential')
        self.assertNotIn('realm_client', client['attributes'], 'Derived native metadata was persisted')
        self.assertEqual('true', client['attributes']['backchannel.logout.session.required'])
        self.assertEqual('false', client['attributes']['backchannel.logout.revoke.offline.tokens'])
        converted = self.run_cli('--apply', '--confirm-isolated-realm', '--receivers-only')
        self.assertEqual(0, converted.returncode, converted.stderr)
        self.assertNotIn('realm_client', client['attributes'])

    def test_pending_native_timestamp_is_strict_and_unknown_attributes_refuse(self):
        client, _ = self.pending_after_failure()
        attrs = dict(client['attributes'])
        for value in (None, True, 1792616400, '', '0', '-1', '01', '2147483648', '1.5', 'unknown'):
            with self.subTest(value=value):
                client['attributes'] = {**attrs, 'client.secret.creation.time': value}
                writes = self.realm.writes
                result = self.run_cli('--apply', '--confirm-isolated-realm')
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(writes, self.realm.writes)
        client['attributes'] = {**attrs, 'unknown-metadata': 'not-authorized'}
        writes = self.realm.writes
        self.assertNotEqual(0, self.run_cli('--apply', '--confirm-isolated-realm').returncode)
        self.assertEqual(writes, self.realm.writes)

    def test_pending_named_native_defaults_cannot_change_values_or_types(self):
        client, _ = self.pending_after_failure()
        attrs = dict(client['attributes'])
        for name in ('backchannel.logout.session.required', 'backchannel.logout.revoke.offline.tokens', 'realm_client'):
            for value in ('unreviewed', True, None):
                with self.subTest(name=name, value=value):
                    client['attributes'] = {**attrs, name: value}
                    writes = self.realm.writes
                    result = self.run_cli('--apply', '--confirm-isolated-realm')
                    self.assertNotEqual(0, result.returncode)
                    self.assertEqual(writes, self.realm.writes)

    def test_failed_final_receipt_put_keeps_pending_and_native_subject_for_retry(self):
        client, subject = self.pending_after_failure('fail_before_subject_receipt')
        resumed = self.run_cli('--apply', '--confirm-isolated-realm')
        self.assertEqual(0, resumed.returncode, resumed.stderr)
        self.assertEqual(subject, client['attributes']['oriso-c.prepared-subject'])
        self.assertEqual(5, len(self.realm.users))

    def test_lost_final_receipt_reply_keeps_completed_subject_and_read_only_retry(self):
        self.realm.fail_after_subject_receipt = True
        failed = self.run_cli('--apply', '--confirm-isolated-realm')
        self.assertNotEqual(0, failed.returncode)
        client = self.realm.clients['backend-setup']
        subject = self.realm.users['backend-setup']['id']
        self.assertNotIn('oriso-c.pending-receipt', client['attributes'])
        self.assertEqual(subject, client['attributes']['oriso-c.prepared-subject'])
        writes, reads = self.realm.writes, self.realm.service_account_reads
        self.assertEqual(0, self.run_cli().returncode)
        self.assertEqual((writes, reads), (self.realm.writes, self.realm.service_account_reads))
        resumed = self.run_cli('--apply', '--confirm-isolated-realm')
        self.assertEqual(0, resumed.returncode, resumed.stderr)
        self.assertEqual(subject, client['attributes']['oriso-c.prepared-subject'])
        self.assertEqual(5, len(self.realm.users))

    def test_wrong_pending_contract_refuses_without_writes(self):
        client, _ = self.pending_after_failure()
        original = client['attributes']['oriso-c.pending-receipt']
        for field in ('version', 'actor', 'clientId', 'realmId', 'realm', 'registrySha256', 'mappingSha256'):
            with self.subTest(field=field):
                marker = json.loads(original)
                marker[field] = 'unreviewed'
                client['attributes']['oriso-c.pending-receipt'] = json.dumps(marker)
                writes = self.realm.writes
                result = self.run_cli('--apply', '--confirm-isolated-realm')
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(writes, self.realm.writes)
        client['attributes']['oriso-c.pending-receipt'] = original

    def test_pending_custom_grants_or_active_flow_refuse_without_writes(self):
        client, _ = self.pending_after_failure()
        self.realm.roles['config-wizard'] = {'id': 'config-wizard', 'name': 'config-wizard'}
        original = json.loads(json.dumps(client))
        for field, value in (('enabled', True), ('implicitFlowEnabled', True),
                             ('scopeRoles', ['config-wizard']),
                             ('defaultClientScopes', ['unreviewed']),
                             ('protocolMappers', [{'name': 'unreviewed'}])):
            with self.subTest(field=field):
                client.clear(); client.update(json.loads(json.dumps(original)))
                client[field] = value
                writes = self.realm.writes
                result = self.run_cli('--apply', '--confirm-isolated-realm')
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(writes, self.realm.writes)

    def test_pending_custom_subject_roles_or_membership_refuse_without_writes(self):
        self.pending_after_failure()
        user = self.realm.users['backend-setup']
        for field, value in (('realmRoles', ['consultant']), ('groups', ['group-1']),
                             ('clientRoles', [{'id': 'foreign', 'name': 'manage-users'}])):
            with self.subTest(field=field):
                user[field] = value
                writes = self.realm.writes
                result = self.run_cli('--apply', '--confirm-isolated-realm')
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(writes, self.realm.writes)
                user[field] = []

    def test_pending_subject_shape_drift_before_final_receipt_refuses(self):
        self.pending_after_failure()
        self.realm.drift_on_native_subject_read = True
        writes = self.realm.writes
        result = self.run_cli('--apply', '--confirm-isolated-realm')
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(writes, self.realm.writes)
        self.assertIn('oriso-c.pending-receipt', self.realm.clients['backend-setup']['attributes'])

    def test_native_empty_prefix_normalization_converges_without_allowing_a_prefix(
        self,
    ):
        result = self.run_cli("--apply", "--confirm-isolated-realm")
        self.assertEqual(0, result.returncode, result.stderr)
        actors = json.loads(
            (ROOT / "files/scenario-c-task-identities.json").read_text()
        )["actors"]
        for actor in actors:
            client = self.realm.clients[actor["clientId"]]
            mapper = next(
                row
                for row in client["protocolMappers"]
                if row["name"] == "oriso-c-realm-roles"
            )
            self.assertNotIn("usermodel.realmRoleMapping.rolePrefix", mapper["config"])
        second = self.run_cli("--apply", "--confirm-isolated-realm")
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(0, json.loads(second.stdout)["changes"])
        mapper["config"]["usermodel.realmRoleMapping.rolePrefix"] = "unexpected-"
        writes = self.realm.writes
        refused = self.run_cli("--apply", "--confirm-isolated-realm")
        self.assertNotEqual(0, refused.returncode)
        self.assertIn(
            "Actor token mapper is not an exact reviewed mapper", refused.stderr
        )
        self.assertEqual(writes, self.realm.writes)

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

    def test_receiver_only_preparation_removes_native_account_paths_and_converges(self):
        prepared = self.run_cli("--apply", "--confirm-isolated-realm")
        self.assertEqual(0, prepared.returncode, prepared.stderr)
        fenced = self.run_cli("--apply", "--confirm-isolated-realm", "--receivers-only")
        self.assertEqual(0, fenced.returncode, fenced.stderr)
        self.assertEqual("RECEIVER_ONLY", json.loads(fenced.stdout)["policy_model"])
        self.assertFalse(json.loads(fenced.stdout)["runtime_cutover"])
        for policy in self.realm.policies.values():
            if policy["type"] == "user":
                self.assertEqual(
                    [],
                    policy.get("users"),
                    "Native account policy still authorizes a subject",
                )
            else:
                self.assertEqual(["oriso-c-deny-everyone"], policy["policies"])
        self.assertTrue(
            all(
                not client["enabled"]
                for client in self.realm.clients.values()
                if client["clientId"].startswith("backend-")
            )
        )
        writes = self.realm.writes
        repeated = self.run_cli(
            "--apply", "--confirm-isolated-realm", "--receivers-only"
        )
        self.assertEqual(0, repeated.returncode, repeated.stderr)
        self.assertEqual(0, json.loads(repeated.stdout)["changes"])
        self.assertEqual(writes, self.realm.writes)

    def test_only_creator_is_provisioner_while_maintenance_policy_stays_separate(self):
        result = self.run_cli("--apply", "--confirm-isolated-realm")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(5, json.loads(result.stdout)["actor_count"])
        creator = self.realm.users["backend-account-create"]["id"]
        maintainer = self.realm.users["backend-account-admin"]["id"]
        self.assertEqual(
            [creator], self.realm.policies["oriso-c-is-provisioner"]["users"]
        )
        self.assertEqual(
            [maintainer], self.realm.policies["oriso-c-is-maintainer"]["users"]
        )
        self.assertEqual(
            ["oriso-c-is-maintainer"],
            self.realm.policies["oriso-c-human-members"]["policies"],
        )
        self.assertEqual(
            ["oriso-c-is-provisioner"],
            self.realm.policies["oriso-c-pending"]["policies"],
        )

    def test_disabled_actor_token_contract_is_prepared_with_exact_roles_and_audiences(
        self,
    ):
        first = self.run_cli("--apply", "--confirm-isolated-realm", "--receivers-only")
        self.assertEqual(0, first.returncode, first.stderr)
        expected = {
            "backend-setup": (
                {"config-wizard", "invitation-reservations", "consultant-import"},
                {
                    "tenantservice",
                    "agencyservice",
                    "consultingtypeservice",
                    "userservice",
                },
            ),
            "backend-account-create": (
                {"account-provisioning", "account-read"},
                {"userservice", "oriso-account-lifecycle"},
            ),
            "backend-account-admin": (
                {
                    "account-maintenance",
                    "account-read",
                    "otp-config-admin",
                    "session-exchange",
                },
                {"userservice", "oriso-account-lifecycle", "oriso-otp"},
            ),
            "backend-mail": (
                {
                    "smtp-sync",
                    "system-email-delivery",
                    "notification-dispatch",
                    "notifications-technical",
                },
                {
                    "tenantservice",
                    "agencyservice",
                    "consultingtypeservice",
                    "userservice",
                    "oriso-task-commands",
                },
            ),
            "backend-runtime": (
                {
                    "runtime-policy",
                    "matrix-agency",
                    "matrix-agency-provision",
                    "appointment-sync",
                    "appointment-participant-cleanup",
                },
                {"tenantservice", "agencyservice", "appointmentservice"},
            ),
        }
        for client_id, (roles, audiences) in expected.items():
            with self.subTest(client_id=client_id):
                client = self.realm.clients[client_id]
                self.assertFalse(client["enabled"])
                self.assertFalse(client["fullScopeAllowed"])
                self.assertEqual([], client["defaultClientScopes"])
                self.assertEqual([], client["optionalClientScopes"])
                self.assertEqual(roles, set(client.get("scopeRoles", [])))
                mappers = client.get("protocolMappers", [])
                self.assertEqual(len(audiences) + 2, len(mappers))
                self.assertEqual(
                    audiences,
                    {
                        m["config"]["included.custom.audience"]
                        for m in mappers
                        if m["protocolMapper"] == "oidc-audience-mapper"
                    },
                )
                self.assertEqual(
                    1, sum(m["protocolMapper"] == "oidc-sub-mapper" for m in mappers)
                )
                self.assertEqual(
                    1,
                    sum(
                        m["protocolMapper"] == "oidc-usermodel-realm-role-mapper"
                        and m["config"]["claim.name"] == "realm_access.roles"
                        for m in mappers
                    ),
                )
        writes = self.realm.writes
        repeated = self.run_cli(
            "--apply", "--confirm-isolated-realm", "--receivers-only"
        )
        self.assertEqual(0, repeated.returncode, repeated.stderr)
        self.assertEqual(0, json.loads(repeated.stdout)["changes"])
        self.assertEqual(writes, self.realm.writes)

    def test_preparation_cannot_silently_restore_native_paths_after_receiver_only(self):
        fenced = self.run_cli("--apply", "--confirm-isolated-realm", "--receivers-only")
        self.assertEqual(0, fenced.returncode, fenced.stderr)
        before = self.realm.writes
        result = self.run_cli("--apply", "--confirm-isolated-realm")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(before, self.realm.writes)
        self.assertNotIn(
            "synthetic-private-installer-token", result.stdout + result.stderr
        )

    def test_native_initial_scopes_are_removed_before_exact_token_preparation_receipt(
        self,
    ):
        self.realm.native_initial_scopes = True
        first = self.run_cli("--apply", "--confirm-isolated-realm", "--receivers-only")
        self.assertEqual(0, first.returncode, first.stderr)
        for client in self.realm.clients.values():
            if not client["clientId"].startswith("backend-"):
                continue
            self.assertEqual([], client["defaultClientScopes"])
            self.assertEqual([], client["optionalClientScopes"])
            self.assertEqual(
                "exact-v1", client["attributes"].get("oriso-c.token-contract")
            )
        before = self.realm.writes
        second = self.run_cli("--apply", "--confirm-isolated-realm", "--receivers-only")
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(0, json.loads(second.stdout)["changes"])
        self.assertEqual(before, self.realm.writes)

    def test_unreviewed_token_sources_refuse_before_any_reconcile_write(self):
        prepared = self.run_cli(
            "--apply", "--confirm-isolated-realm", "--receivers-only"
        )
        self.assertEqual(0, prepared.returncode, prepared.stderr)
        original = json.dumps(self.realm.clients)
        changes = [
            {"defaultClientScopes": ["roles"]},
            {"optionalClientScopes": ["offline_access"]},
            {"scopeRoles": ["tenant-admin"]},
            {
                "scopeClientMappings": {
                    "realm-management": {"mappings": [{"name": "manage-users"}]}
                }
            },
            {
                "protocolMappers": [
                    {
                        "name": "unreviewed",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-script-based-protocol-mapper",
                        "config": {},
                    }
                ]
            },
        ]
        for change in changes:
            with self.subTest(change=change):
                self.realm.clients = json.loads(original)
                self.realm.clients["backend-mail"].update(change)
                before = self.realm.writes
                result = self.run_cli(
                    "--apply", "--confirm-isolated-realm", "--receivers-only"
                )
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(before, self.realm.writes)

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

    def test_unconfirmed_apply_never_reaches_missing_native_service_account(self):
        prepared = self.run_cli("--apply", "--confirm-isolated-realm")
        self.assertEqual(0, prepared.returncode, prepared.stderr)
        self.assertGreater(self.realm.service_account_reads, 0)
        # The managed receipt still exists, but the native association is absent.
        del self.realm.native_service_accounts["backend-mail"]
        before = json.dumps(
            [self.realm.clients, self.realm.users, self.realm.native_service_accounts],
            sort_keys=True,
        )
        self.realm.writes = 0
        self.realm.requests = 0
        self.realm.service_account_reads = 0

        result = self.run_cli("--apply")

        self.assertNotEqual(0, result.returncode)
        self.assertEqual(0, self.realm.lazy_service_account_creations)
        self.assertEqual(0, self.realm.service_account_reads)
        self.assertEqual(0, self.realm.writes)
        self.assertEqual(0, self.realm.requests)
        self.assertIn("Apply requires explicit confirmation", result.stderr)
        self.assertEqual(
            before,
            json.dumps(
                [
                    self.realm.clients,
                    self.realm.users,
                    self.realm.native_service_accounts,
                ],
                sort_keys=True,
            ),
        )

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
    def test_registry_rejects_changed_or_duplicate_bearer_audiences(self):
        data = json.loads((ROOT / "files/scenario-c-task-identities.json").read_text())
        for audiences in (
            ["userservice"],
            [
                "tenantservice",
                "agencyservice",
                "consultingtypeservice",
                "userservice",
                "oriso-task-commands",
                "oriso-task-commands",
            ],
            [
                "tenantservice",
                "agencyservice",
                "consultingtypeservice",
                "userservice",
                "realm-management",
            ],
        ):
            with self.subTest(
                audiences=audiences
            ), tempfile.TemporaryDirectory() as temp:
                data["actors"][3]["bearerAudiences"] = audiences
                registry = Path(temp) / "registry.json"
                registry.write_text(json.dumps(data))
                result = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "--registry",
                        str(registry),
                        "--validate-only",
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(0, result.returncode)

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
