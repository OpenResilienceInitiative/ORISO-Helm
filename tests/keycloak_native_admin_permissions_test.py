#!/usr/bin/env python3
"""Spike ORISO-Keycloak#56: can native fine-grained admin permissions (FGAP v2)
replace the custom account provider?

Real Keycloak, real client_credentials tokens, real Admin REST API. Synthetic
credentials only. Starts and removes its own isolated container (prefix
``spike56-``); skipped when docker is not available. Run separately:

    python3 tests/keycloak_native_admin_permissions_test.py -v

Set SPIKE56_KEYCLOAK_URL=http://127.0.0.1:<port>/auth (master admin
synthetic-installer / synthetic-install-test-credential) to reuse a running
container while iterating.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import unittest
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
# Same pinned 26.6.3 image as tests/keycloak_task_migration_test.py on the option D branch.
IMAGE = "quay.io/keycloak/keycloak@sha256:9b0330756022422149aa6502eb2def8cd47c6e1b000c7c65cdb13e7c0133e992"
REALM = "spike56"
ADMIN_USER = "synthetic-installer"
ADMIN_PASSWORD = "synthetic-install-test-credential"
SOURCE_REALM = ROOT / "charts/keycloak/keycloak-resources/realm.json"

# Roles account-create may hand out (provider contract; extend per role, one map-role grant each).
HUMAN_ROLES = ("user", "consultant")
NATIVE_ROLES = {"offline_access", "uma_authorization"}
TASK_CLIENTS = ("account-create", "account-admin", "backend-admin")
OTP_SECRET = "JBSWY3DPEHPK3PXP"
RESULTS: list[str] = []


def docker_available() -> bool:
    if os.environ.get("SPIKE56_KEYCLOAK_URL"):
        return True
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def call(base, method, path, body=None, token=None, form=False):
    """Return (status, parsed body); never raises on HTTP errors."""
    headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = urlencode(body).encode() if form else json.dumps(body).encode()
    try:
        with urlopen(Request(base + path, data=data, headers=headers, method=method), timeout=30) as response:
            raw = response.read()
            status = response.status
    except HTTPError as error:
        raw, status = error.read(), error.code
        error.close()
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw.decode(errors="replace")
    return status, parsed


def admin_roles_from_chart() -> list[str]:
    """ORISO realm roles from the Helm realm.json that are not human roles."""
    realm = json.loads(SOURCE_REALM.read_text())
    names = [role["name"] for role in realm["roles"]["realm"]]
    return sorted(n for n in names if n not in HUMAN_ROLES and n not in NATIVE_ROLES and not n.startswith("default-roles-"))


def otp_credential():
    return {
        "type": "otp",
        "userLabel": "spike56",
        "secretData": json.dumps({"value": OTP_SECRET}),
        "credentialData": json.dumps({"subType": "totp", "digits": 6, "counter": 0, "period": 30, "algorithm": "HmacSHA1"}),
    }


class Keycloak:
    """Isolated Keycloak plus a seeded spike56 realm."""

    def __init__(self):
        self.name = None
        self.base = os.environ.get("SPIKE56_KEYCLOAK_URL")

    def start(self):
        if not self.base:
            self.name = "spike56-" + uuid.uuid4().hex[:10]
            subprocess.run(["docker", "run", "-d", "--name", self.name, "-p", "127.0.0.1::8080",
                            "-e", "JAVA_OPTS_APPEND=-XX:ActiveProcessorCount=2",
                            "-e", "KC_BOOTSTRAP_ADMIN_USERNAME=" + ADMIN_USER,
                            "-e", "KC_BOOTSTRAP_ADMIN_PASSWORD=" + ADMIN_PASSWORD,
                            IMAGE, "start-dev", "--http-relative-path=/auth"], check=True, capture_output=True)
            endpoint = subprocess.run(["docker", "port", self.name, "8080/tcp"], check=True,
                                      capture_output=True, text=True).stdout.strip()
            self.base = "http://" + endpoint + "/auth"
        deadline = time.monotonic() + 300
        while True:
            try:
                self.admin_token()
                return
            except (URLError, ConnectionError, TimeoutError, AssertionError, OSError):
                if time.monotonic() > deadline:
                    raise AssertionError("isolated Keycloak did not become ready") from None
                time.sleep(2)

    def stop(self):
        if self.name:
            subprocess.run(["docker", "rm", "-f", self.name], check=True, capture_output=True)

    def admin_token(self):
        status, body = call(self.base, "POST", "/realms/master/protocol/openid-connect/token",
                            {"grant_type": "password", "client_id": "admin-cli",
                             "username": ADMIN_USER, "password": ADMIN_PASSWORD}, form=True)
        assert status == 200, (status, body)
        return body["access_token"]

    # -- master-admin helpers (fixture only) -------------------------------------------------
    def admin(self, method, path, body=None):
        status, parsed = call(self.base, method, "/admin/realms/" + REALM + path, body, self.admin_token())
        assert status < 300, (method, path, status, parsed)
        return parsed

    def user_id(self, username):
        return self.admin("GET", "/users?" + urlencode({"username": username, "exact": "true"}))[0]["id"]

    def group_id(self, path):
        return self.admin("GET", "/group-by-path/" + quote(path.lstrip("/")))["id"]

    def role(self, name):
        return self.admin("GET", "/roles/" + quote(name))

    def client_uuid(self, client_id):
        return self.admin("GET", "/clients?" + urlencode({"clientId": client_id}))[0]["id"]

    def service_account(self, client_id):
        return self.admin("GET", "/clients/" + self.client_uuid(client_id) + "/service-account-user")["id"]

    def token(self, client_id):
        status, body = call(self.base, "POST", "/realms/" + REALM + "/protocol/openid-connect/token",
                            {"grant_type": "client_credentials", "client_id": client_id,
                             "client_secret": "spike56-" + client_id}, form=True)
        assert status == 200, (client_id, status, body)
        return body["access_token"]

    def authz(self, method, path, body=None):
        return self.admin(method, "/clients/" + self.client_uuid("admin-permissions") + "/authz/resource-server" + path, body)

    # -- realm seed ----------------------------------------------------------------------------
    def seed(self):
        status, _ = call(self.base, "DELETE", "/admin/realms/" + REALM, token=self.admin_token())
        assert status in (204, 404)
        roles = [{"name": n} for n in HUMAN_ROLES + tuple(admin_roles_from_chart())]
        realm = {
            "realm": REALM,
            "enabled": True,
            "adminPermissionsEnabled": True,
            "roles": {"realm": roles},
            "groups": [
                {"name": "pending-provisioning"},
                # Human roles are carried by groups: joining one is bounded per user, unlike
                # direct role mapping (map-roles cannot be limited to the pending group).
                {"name": "human-roles", "subGroups": [{"name": r, "realmRoles": [r]} for r in HUMAN_ROLES]},
                # Admin roles live only on protected groups, so "has admin role" == "is member".
                {"name": "protected-admins", "subGroups": [
                    {"name": "platform-admins", "realmRoles": ["tenant-admin", "agency-admin", "user-admin"]},
                    {"name": "traeger-admins", "realmRoles": ["single-tenant-admin", "restricted-agency-admin"]},
                    {"name": "technical-identities"},
                ]},
            ],
        }
        status, body = call(self.base, "POST", "/admin/realms", realm, self.admin_token())
        assert status == 201, (status, body)
        self.seed_user("existing-human", groups=["/human-roles/consultant"])
        self.seed_user("platform-admin", groups=["/protected-admins/platform-admins"])
        self.seed_user("traeger-admin", groups=["/protected-admins/traeger-admins"])
        # Drift case: admin role mapped directly, not via the protected group.
        self.seed_user("drifted-admin", roles=["tenant-admin"])
        for client_id in TASK_CLIENTS:
            self.admin("POST", "/clients", {
                "clientId": client_id, "protocol": "openid-connect", "publicClient": False,
                "secret": "spike56-" + client_id, "serviceAccountsEnabled": True,
                "standardFlowEnabled": False, "directAccessGrantsEnabled": False, "enabled": True})
            self.admin("PUT", "/users/" + self.service_account(client_id) + "/groups/"
                       + self.group_id("/protected-admins/technical-identities"))
        # backend-admin mirrors dev today: realm-management manage-users, view-users, query-users, view-realm.
        management = self.client_uuid("realm-management")
        legacy = [self.admin("GET", "/clients/" + management + "/roles/" + r)
                  for r in ("manage-users", "view-users", "query-users", "view-realm")]
        self.admin("POST", "/users/" + self.service_account("backend-admin") + "/role-mappings/clients/" + management, legacy)
        self.permissions()

    def seed_user(self, username, groups=(), roles=(), otp=True):
        user = {"username": username, "enabled": True, "email": username + "@example.invalid",
                "groups": list(groups), "realmRoles": list(roles),
                "credentials": [otp_credential()] if otp else []}
        self.admin("POST", "/partialImport", {"ifResourceExists": "FAIL", "users": [user]})
        return self.user_id(username)

    def otp_id(self, user):
        return next(c["id"] for c in self.admin("GET", "/users/" + user + "/credentials") if c["type"] == "otp")

    def permissions(self):
        """FGAP v2 rules measured in this spike (see test_semantics_*):
        - a permission on (resource, scope) DENIES that scope to every principal its policy does not match;
        - ANY all-users permission for other principals removes group-granted user access entirely.
        So: no all-users permission at all; reach is granted per group, one permission per
        (group, scope set), and the policy lists every principal that needs it."""
        sa = {c: self.service_account(c) for c in TASK_CLIENTS}
        policies = {
            "is-maintainer": [sa["account-admin"], sa["backend-admin"]],
            "is-provisioner": [sa[c] for c in TASK_CLIENTS],
            "deny-everyone": [],
        }
        for name, users in policies.items():
            self.authz("POST", "/policy/user", {"name": name, "logic": "POSITIVE", "users": users})
        group = self.group_id
        role_groups = [group("/human-roles")] + [group("/human-roles/" + r) for r in HUMAN_ROLES]
        for name, resources, scopes, policy in (
            # account-create reaches only users in the pending group.
            ("pending-group", [group("/pending-provisioning")],
             ["view", "view-members", "manage-members", "manage-membership", "manage-membership-of-members"],
             "is-provisioner"),
            # Joining a human-role group = commit; it also needs the pending-group bridge on the user.
            ("human-role-groups-join", role_groups, ["view", "manage-membership"], "is-provisioner"),
            # Maintainers reach only human-role members (allow-list, fails closed for everyone else).
            ("human-role-members", role_groups,
             ["view-members", "manage-members", "manage-membership-of-members"], "is-maintainer"),
            # Member deny wins over other grants and cascades to subgroups.
            ("protect-admins", [group("/protected-admins")],
             ["view", "view-members", "manage-members", "impersonate-members", "manage-membership",
              "manage-membership-of-members"], "deny-everyone"),
        ):
            self.authz("POST", "/permission/scope", {"name": name, "resourceType": "Groups", "resources": resources,
                                                     "scopes": scopes, "policies": [policy]})


@unittest.skipUnless(docker_available(), "docker not available")
class NativeAdminPermissionsSpikeTest(unittest.TestCase):
    kc: Keycloak

    @classmethod
    def setUpClass(cls):
        cls.kc = Keycloak()
        cls.kc.start()
        try:
            cls.kc.seed()
        except Exception:
            cls.kc.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.kc.stop()
        print("\n".join(["", "Spike #56 evidence (status per real-token call):"] + RESULTS))

    def api(self, client_id, method, path, body=None):
        status, parsed = call(self.kc.base, method, "/admin/realms/" + REALM + path, body, self.kc.token(client_id))
        RESULTS.append(f"  {self._testMethodName}: {client_id} {method} {self.label(path)} -> {status}")
        return status, parsed

    def label(self, path):
        names = getattr(self.kc, "names", {})
        return "/".join(names.get(part, part) for part in path.split("?")[0].split("/"))

    def expect(self, expected, client_id, method, path, body=None):
        status, parsed = self.api(client_id, method, path, body)
        self.assertEqual(expected, status, f"{client_id} {method} {self.label(path)}: {parsed}")
        return parsed

    def name(self, ident, label):
        self.kc.names = getattr(self.kc, "names", {})
        self.kc.names[ident] = "{" + label + "}"
        return ident

    def user(self, username):
        return self.name(self.kc.user_id(username), username)

    def group(self, path):
        return self.name(self.kc.group_id(path), path.rsplit("/", 1)[-1])

    def create_pending(self):
        username = "spike-" + uuid.uuid4().hex[:8]
        self.expect(201, "account-create", "POST", "/users",
                    {"username": username, "enabled": True, "groups": ["/pending-provisioning"]})
        return self.name(self.kc.user_id(username), "new-pending-user")

    def effective_roles(self, user):
        return {r["name"] for r in self.kc.admin("GET", "/users/" + user + "/role-mappings/realm/composite")}

    # -- Guarantee 1: create with human roles only ------------------------------------------------
    def test_g1_create_needs_no_realm_wide_manage_only_the_pending_group(self):
        self.expect(403, "account-create", "POST", "/users", {"username": "spike-nogroup", "enabled": True})
        self.create_pending()
        self.expect(403, "account-create", "POST", "/users",
                    {"username": "spike-two-groups", "enabled": True,
                     "groups": ["/pending-provisioning", "/protected-admins/platform-admins"]})

    def test_g1_human_role_via_role_group_but_no_admin_roles(self):
        user = self.create_pending()
        for path in ("/protected-admins", "/protected-admins/platform-admins", "/protected-admins/traeger-admins"):
            self.expect(403, "account-create", "PUT", "/users/" + user + "/groups/" + self.group(path))
        for name in HUMAN_ROLES + tuple(admin_roles_from_chart()):
            self.expect(403, "account-create", "POST", "/users/" + user + "/role-mappings/realm", [self.kc.role(name)])
        management = self.kc.client_uuid("realm-management")
        realm_admin = self.kc.admin("GET", "/clients/" + management + "/roles/realm-admin")
        self.expect(403, "account-create", "POST", "/users/" + user + "/role-mappings/clients/" + management, [realm_admin])
        self.expect(204, "account-create", "PUT", "/users/" + user + "/groups/" + self.group("/human-roles/consultant"))
        roles = self.effective_roles(user)
        self.assertIn("consultant", roles)
        self.assertFalse(roles & set(admin_roles_from_chart()), roles)

    def test_g1_realm_roles_in_create_body_are_ignored(self):
        self.expect(201, "account-create", "POST", "/users",
                    {"username": "spike-body-roles", "enabled": True, "groups": ["/pending-provisioning"],
                     "realmRoles": ["tenant-admin"]})
        self.assertNotIn("tenant-admin", self.effective_roles(self.kc.user_id("spike-body-roles")))

    # -- Guarantee 2: no change/delete of users it did not create ----------------------------------
    def test_g2_account_create_cannot_touch_existing_users(self):
        for username in ("existing-human", "platform-admin", "drifted-admin"):
            user = self.user(username)
            self.expect(403, "account-create", "GET", "/users/" + user)
            self.expect(403, "account-create", "PUT", "/users/" + user, {"firstName": "changed"})
            self.expect(403, "account-create", "DELETE", "/users/" + user)
            self.expect(403, "account-create", "PUT", "/users/" + user + "/reset-password",
                        {"type": "password", "value": "Spike56-reset-2026!", "temporary": True})
            self.expect(403, "account-create", "DELETE", "/users/" + user + "/credentials/" + self.kc.otp_id(user))
            self.expect(403, "account-create", "POST", "/users/" + user + "/role-mappings/realm", [self.kc.role("user")])
            # Escape attempts: pull the user into the pending group or a human-role group.
            self.expect(403, "account-create", "PUT", "/users/" + user + "/groups/" + self.group("/pending-provisioning"))
            self.expect(403, "account-create", "PUT", "/users/" + user + "/groups/" + self.group("/human-roles/user"))
        human = self.user("existing-human")
        self.expect(403, "account-create", "DELETE", "/users/" + human + "/groups/" + self.group("/human-roles/consultant"))
        self.expect(403, "account-create", "GET", "/groups/" + self.group("/human-roles/consultant") + "/members")

    def test_g2_existing_human_moved_into_pending_stays_out_of_account_create_reach(self):
        human = self.name(self.kc.seed_user("g2-handback", groups=["/human-roles/consultant"]), "g2-handback")
        self.expect(204, "account-admin", "PUT", "/users/" + human + "/groups/" + self.group("/pending-provisioning"))
        self.expect(403, "account-create", "GET", "/users/" + human)
        self.expect(403, "account-create", "DELETE", "/users/" + human)
        self.expect(403, "account-create", "PUT", "/users/" + human, {"firstName": "taken-over"})

    # -- Guarantee 3: undo own unfinished creation -------------------------------------------------
    def test_g3_account_create_deletes_its_unfinished_creation(self):
        user = self.create_pending()
        self.expect(204, "account-create", "PUT", "/users/" + user, {"firstName": "in-progress", "email": user + "@example.invalid"})
        self.expect(204, "account-create", "PUT", "/users/" + user + "/reset-password",
                    {"type": "password", "value": "Spike56-initial-2026!", "temporary": True})
        self.expect(204, "account-create", "DELETE", "/users/" + user)
        status, _ = call(self.kc.base, "GET", "/admin/realms/" + REALM + "/users/" + user, token=self.kc.admin_token())
        self.assertEqual(404, status)

    def test_g3_joining_a_role_group_commits_and_ends_account_create_reach(self):
        user = self.create_pending()
        self.expect(204, "account-create", "PUT", "/users/" + user + "/groups/" + self.group("/human-roles/consultant"))
        self.expect(403, "account-create", "DELETE", "/users/" + user)
        self.expect(403, "account-create", "PUT", "/users/" + user, {"firstName": "late"})
        self.expect(403, "account-create", "PUT", "/users/" + user + "/groups/" + self.group("/human-roles/user"))
        self.expect(403, "account-create", "DELETE", "/users/" + user + "/groups/" + self.group("/pending-provisioning"))
        # The pending marker is cleared by a maintainer; then it is an ordinary human account.
        self.expect(204, "account-admin", "DELETE", "/users/" + user + "/groups/" + self.group("/pending-provisioning"))
        self.expect(204, "account-admin", "PUT", "/users/" + user, {"firstName": "maintained"})
        self.assertIn("consultant", self.effective_roles(user))

    # -- Guarantee 4: account-admin cannot touch admin accounts or admin roles ---------------------
    def test_g4_account_admin_maintains_humans(self):
        human = self.name(self.kc.seed_user("g4-human", groups=["/human-roles/consultant"]), "g4-human")
        self.expect(200, "account-admin", "GET", "/users/" + human)
        self.expect(204, "account-admin", "PUT", "/users/" + human, {"firstName": "maintained"})
        self.expect(204, "account-admin", "PUT", "/users/" + human + "/groups/" + self.group("/human-roles/user"))
        self.expect(204, "account-admin", "DELETE", "/users/" + human + "/groups/" + self.group("/human-roles/consultant"))
        self.assertEqual({"user"}, self.effective_roles(human) & set(HUMAN_ROLES))
        self.expect(204, "account-admin", "DELETE", "/users/" + human)
        self.expect(201, "account-admin", "POST", "/users",
                    {"username": "g4-created", "enabled": True, "groups": ["/human-roles/user"]})

    def test_g4_account_admin_cannot_touch_protected_admins(self):
        for username in ("platform-admin", "traeger-admin"):
            user = self.user(username)
            self.expect(403, "account-admin", "GET", "/users/" + user)
            self.expect(403, "account-admin", "PUT", "/users/" + user, {"enabled": False})
            self.expect(403, "account-admin", "DELETE", "/users/" + user)
            self.expect(403, "account-admin", "POST", "/users/" + user + "/role-mappings/realm", [self.kc.role("consultant")])
            self.expect(403, "account-admin", "PUT", "/users/" + user + "/groups/" + self.group("/human-roles/user"))
            self.expect(403, "account-admin", "POST", "/users/" + user + "/impersonation")
        platform = self.user("platform-admin")
        self.expect(403, "account-admin", "DELETE", "/users/" + platform + "/groups/" + self.group("/protected-admins/platform-admins"))

    def test_g4_account_admin_cannot_grant_admin_roles(self):
        human = self.name(self.kc.seed_user("g4-escalate", groups=["/human-roles/user"]), "g4-escalate")
        for path in ("/protected-admins", "/protected-admins/platform-admins", "/protected-admins/traeger-admins"):
            self.expect(403, "account-admin", "PUT", "/users/" + human + "/groups/" + self.group(path))
        for name in admin_roles_from_chart():
            self.expect(403, "account-admin", "POST", "/users/" + human + "/role-mappings/realm", [self.kc.role(name)])
        management = self.kc.client_uuid("realm-management")
        for name in ("realm-admin", "manage-users"):
            role = self.kc.admin("GET", "/clients/" + management + "/roles/" + name)
            self.expect(403, "account-admin", "POST", "/users/" + human + "/role-mappings/clients/" + management, [role])
        self.expect(403, "account-admin", "POST", "/users",
                    {"username": "g4-admin-create", "enabled": True, "groups": ["/protected-admins/platform-admins"]})
        self.assertFalse(self.effective_roles(human) & set(admin_roles_from_chart()))

    def test_g4_account_admin_cannot_touch_task_service_accounts(self):
        for client_id in TASK_CLIENTS:
            subject = self.name(self.kc.service_account(client_id), "service-account-" + client_id)
            self.expect(403, "account-admin", "PUT", "/users/" + subject, {"enabled": False})
            self.expect(403, "account-admin", "DELETE", "/users/" + subject)

    def test_g4_users_outside_human_role_groups_are_unreachable(self):
        # Fails closed: an admin role mapped directly (drift) is still out of reach; so is any legacy
        # user whose human role is mapped directly instead of through a human-role group.
        for username in ("drifted-admin",):
            user = self.user(username)
            self.expect(403, "account-admin", "PUT", "/users/" + user, {"firstName": "unprotected"})
        legacy = self.name(self.kc.seed_user("legacy-direct-role", roles=["consultant"]), "legacy-direct-role")
        self.expect(403, "account-admin", "PUT", "/users/" + legacy, {"firstName": "unreachable"})

    # -- Guarantee 5: OTP / credential reset limited ------------------------------------------------
    def test_g5_otp_and_password_reset_only_for_permitted_users(self):
        human = self.name(self.kc.seed_user("g5-human", groups=["/human-roles/consultant"]), "g5-human")
        reset = {"type": "password", "value": "Spike56-reset-2026!", "temporary": True}
        self.expect(403, "account-create", "DELETE", "/users/" + human + "/credentials/" + self.kc.otp_id(human))
        self.expect(200, "account-admin", "GET", "/users/" + human + "/credentials")
        self.expect(204, "account-admin", "PUT", "/users/" + human + "/reset-password", reset)
        self.expect(204, "account-admin", "DELETE", "/users/" + human + "/credentials/" + self.kc.otp_id(human))
        for username in ("platform-admin", "traeger-admin"):
            user = self.user(username)
            self.expect(403, "account-admin", "GET", "/users/" + user + "/credentials")
            self.expect(403, "account-admin", "PUT", "/users/" + user + "/reset-password", reset)
            self.expect(403, "account-admin", "DELETE", "/users/" + user + "/credentials/" + self.kc.otp_id(user))
            self.expect(403, "account-admin", "PUT", "/users/" + user + "/execute-actions-email", ["CONFIGURE_TOTP"])

    # -- Guarantee 6: backend-admin limited the same way -------------------------------------------
    def test_g6_backend_admin_admin_roles_bypass_fgap_until_removed(self):
        platform = self.user("platform-admin")
        human = self.name(self.kc.seed_user("g6-human", groups=["/human-roles/consultant"]), "g6-human")
        # Today (realm-management manage-users): FGAP is bypassed, protected admins are reachable.
        self.expect(204, "backend-admin", "PUT", "/users/" + platform, {"firstName": "bypassed"})
        self.expect(204, "backend-admin", "POST", "/users/" + human + "/role-mappings/realm", [self.kc.role("tenant-admin")])
        self.kc.admin("DELETE", "/users/" + human + "/role-mappings/realm", [self.kc.role("tenant-admin")])
        # Quick win: drop manage-users + view-users, keep query-users + view-realm; FGAP now applies.
        management = self.kc.client_uuid("realm-management")
        broad = [self.kc.admin("GET", "/clients/" + management + "/roles/" + r) for r in ("manage-users", "view-users")]
        self.kc.admin("DELETE", "/users/" + self.kc.service_account("backend-admin") + "/role-mappings/clients/" + management, broad)
        self.expect(403, "backend-admin", "PUT", "/users/" + platform, {"firstName": "blocked"})
        self.expect(403, "backend-admin", "POST", "/users/" + human + "/role-mappings/realm", [self.kc.role("tenant-admin")])
        self.expect(403, "backend-admin", "PUT", "/users/" + human + "/groups/" + self.group("/protected-admins/platform-admins"))
        self.expect(204, "backend-admin", "PUT", "/users/" + human, {"firstName": "maintained"})
        self.expect(204, "backend-admin", "PUT", "/users/" + human + "/groups/" + self.group("/human-roles/user"))
        self.expect(201, "backend-admin", "POST", "/users", {"username": "g6-created", "enabled": True, "groups": ["/human-roles/user"]})
        self.expect(403, "backend-admin", "POST", "/users", {"username": "g6-groupless", "enabled": True})
        found = self.expect(200, "backend-admin", "GET", "/users?" + urlencode({"username": "g6-human", "exact": "true"}))
        self.assertEqual(1, len(found))
        names = {u["username"] for u in self.expect(200, "backend-admin", "GET", "/users?max=200")}
        self.assertFalse(names & {"platform-admin", "traeger-admin", "drifted-admin"}, names)

    # -- Measured FGAP v2 semantics the design depends on -------------------------------------------
    def test_semantics_any_all_users_permission_for_others_removes_group_reach(self):
        user = self.create_pending()
        self.expect(200, "account-create", "GET", "/users/" + user)
        probe = self.kc.authz("POST", "/permission/scope", {
            "name": "probe-all-users", "resourceType": "Users", "scopes": ["map-roles"], "policies": ["is-maintainer"]})
        try:
            self.expect(403, "account-create", "GET", "/users/" + user)
        finally:
            self.kc.authz("DELETE", "/permission/scope/" + probe["id"])
        self.expect(200, "account-create", "GET", "/users/" + user)

    def test_semantics_group_permission_for_one_principal_denies_same_scope_to_others(self):
        human = self.name(self.kc.seed_user("sem-human", groups=["/human-roles/consultant"]), "sem-human")
        self.expect(204, "account-admin", "PUT", "/users/" + human, {"firstName": "before"})
        policy = self.kc.authz("POST", "/policy/user", {"name": "probe-only-account-create", "logic": "POSITIVE",
                                                        "users": [self.kc.service_account("account-create")]})
        probe = self.kc.authz("POST", "/permission/scope", {
            "name": "probe-other-principal", "resourceType": "Groups",
            "resources": [self.kc.group_id("/human-roles/consultant")],
            "scopes": ["manage-members"], "policies": ["probe-only-account-create"]})
        try:
            self.expect(403, "account-admin", "PUT", "/users/" + human, {"firstName": "during"})
        finally:
            self.kc.authz("DELETE", "/permission/scope/" + probe["id"])
            self.kc.authz("DELETE", "/policy/user/" + policy["id"])
        self.expect(204, "account-admin", "PUT", "/users/" + human, {"firstName": "after"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
