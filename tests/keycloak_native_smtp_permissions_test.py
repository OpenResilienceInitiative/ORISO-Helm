#!/usr/bin/env python3
"""Real-token capability probe for Keycloak#57; this does not approve a rollout.

Run with ORISO_REQUIRE_KEYCLOAK_DOCKER=1 to prohibit skipped receiver proof.
The mail fixture has only an application role; delivery itself is a different
application seam and is not claimed by this native-Keycloak probe.
"""
import unittest
import uuid
from urllib.parse import urlencode

import _keycloak_container as shared
from keycloak_native_admin_permissions_test import call


SMTP = {"host": "smtp.example.invalid", "port": "587", "from": "mail@example.invalid"}
RESULTS = []


class NativeSmtpPermissionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        RESULTS.clear()
        cls.container = shared.KeycloakContainer("smtp57").start()
        cls.addClassCleanup(cls.container.stop)
        cls.base = cls.container.base

    @classmethod
    def tearDownClass(cls):
        print("\nKeycloak57 real-token evidence:\n" + "\n".join(RESULTS))

    def setUp(self):
        self.realm = "smtp57-" + uuid.uuid4().hex[:10]
        self.foreign = self.realm + "-foreign"
        self.installer = shared.master_admin(self.base)
        for name in (self.realm, self.foreign):
            status, _ = call(self.base, "POST", "/admin/realms", {
                "realm": name, "enabled": True, "adminPermissionsEnabled": True,
                "displayName": "unchanged", "registrationAllowed": False,
                "smtpServer": SMTP, "roles": {"realm": [{"name": "system-email-delivery"}]},
            }, self.installer)
            self.assertEqual(201, status)
            self.addCleanup(call, self.base, "DELETE", "/admin/realms/" + name, token=self.installer)
        for client in ("mail", "smtp-sync"):
            self.admin("POST", "/clients", {
                "clientId": client, "secret": "synthetic-smtp57-" + client,
                "publicClient": False, "serviceAccountsEnabled": True,
                "standardFlowEnabled": False, "directAccessGrantsEnabled": False,
                "fullScopeAllowed": True,
            })
        self.management = self.client_id("realm-management")
        self.smtp_subject = self.subject("smtp-sync")
        role = self.admin("GET", "/clients/" + self.management + "/roles/manage-realm")
        self.admin("POST", "/users/" + self.smtp_subject + "/role-mappings/clients/" + self.management, [role])
        self.assertEqual(["manage-realm"], shared.claims(self.token("smtp-sync"))["resource_access"]
                         ["realm-management"]["roles"])
        self.assertTrue(self.admin("GET", "")["adminPermissionsEnabled"])
        mail_role = self.admin("GET", "/roles/system-email-delivery")
        self.admin("POST", "/users/" + self.subject("mail") + "/role-mappings/realm", [mail_role])
        self.admin("POST", "/users", {"username": "existing-human", "enabled": True})
        self.human = self.admin("GET", "/users?username=existing-human&exact=true")[0]["id"]

    def admin(self, method, path, body=None):
        status, parsed = call(self.base, method, "/admin/realms/" + self.realm + path, body, self.installer)
        self.assertLess(status, 300, (method, path, status))
        return parsed

    def client_id(self, client):
        return self.admin("GET", "/clients?" + urlencode({"clientId": client}))[0]["id"]

    def subject(self, client):
        return self.admin("GET", "/clients/" + self.client_id(client) + "/service-account-user")["id"]

    def token(self, client):
        status, body = call(self.base, "POST", "/realms/" + self.realm + "/protocol/openid-connect/token", {
            "grant_type": "client_credentials", "client_id": client,
            "client_secret": "synthetic-smtp57-" + client,
        }, form=True)
        self.assertEqual(200, status, (client, status))
        return body["access_token"]

    def api(self, client, method, path="", body=None, realm=None):
        status, parsed = call(self.base, method, "/admin/realms/" + (realm or self.realm) + path,
                              body, self.token(client))
        RESULTS.append(f"{self._testMethodName}: {client} {method} {path or '/realm'} -> {status}")
        return status, parsed

    def expect(self, status, client, method, path="", body=None, realm=None):
        actual, parsed = self.api(client, method, path, body, realm)
        self.assertEqual(status, actual, (client, method, path, actual))
        return parsed

    def authz_path(self):
        return "/clients/" + self.client_id("admin-permissions") + "/authz/resource-server"

    def test_application_mail_role_has_no_native_administration(self):
        claims = shared.claims(self.token("mail"))
        self.assertIn("system-email-delivery", claims.get("realm_access", {}).get("roles", []))
        self.assertNotIn("realm-management", claims.get("resource_access", {}))
        role = self.admin("GET", "/roles/system-email-delivery")
        authz = self.authz_path()
        for method, path, body in (
            ("GET", "", None),
            ("PUT", "", {"smtpServer": dict(SMTP, host="denied.example.invalid")}),
            ("PUT", "", {"registrationAllowed": True}),
            ("GET", "/users", None),
            ("POST", "/users", {"username": "denied-user"}),
            ("PUT", "/users/" + self.human, {"firstName": "denied"}),
            ("DELETE", "/users/" + self.human, None),
            ("POST", "/users/" + self.human + "/role-mappings/realm", [role]),
            ("PUT", "/users/" + self.human + "/reset-password",
             {"type": "password", "value": "Synthetic-mail57-denied!", "temporary": True}),
            ("POST", "/roles", {"name": "denied-role"}),
            ("POST", authz + "/policy/user", {"name": "mail-policy", "users": [self.subject("mail")]}),
        ):
            with self.subTest(method=method, path=path):
                self.expect(403, "mail", method, path, body)
        self.assertEqual(SMTP, self.admin("GET", "")["smtpServer"])
        self.assertFalse(self.admin("GET", "")["registrationAllowed"])
        self.assertNotIn("firstName", self.admin("GET", "/users/" + self.human))
        self.assertEqual([], self.admin("GET", "/users?username=denied-user&exact=true"))

    def test_manage_realm_updates_smtp_but_also_unrelated_and_mixed_fields(self):
        changed_smtp = dict(SMTP, host="changed.example.invalid")
        self.expect(204, "smtp-sync", "PUT", body={"smtpServer": changed_smtp})
        self.assertEqual(changed_smtp, self.admin("GET", "")["smtpServer"])
        self.expect(204, "smtp-sync", "PUT", body={"registrationAllowed": True})
        self.assertTrue(self.admin("GET", "")["registrationAllowed"])
        # This 204 is a measured GAP, not the desired SMTP-only contract. The
        # original candidate assertion (403) failed against the real receiver.
        self.expect(204, "smtp-sync", "PUT", body={"smtpServer": SMTP, "displayName": "unrelated-change"})
        actual = self.admin("GET", "")
        self.assertEqual(SMTP, actual["smtpServer"])
        self.assertEqual("unrelated-change", actual["displayName"])

    def test_manage_realm_is_confined_to_its_own_realm(self):
        for client in ("mail", "smtp-sync"):
            status, _ = self.api(client, "PUT", body={"smtpServer": dict(SMTP, host="foreign-change.invalid")},
                                 realm=self.foreign)
            self.assertIn(status, (401, 403))
        status, foreign = call(self.base, "GET", "/admin/realms/" + self.foreign, token=self.installer)
        self.assertEqual(200, status)
        self.assertEqual(SMTP, foreign["smtpServer"])
        status, _ = self.api("smtp-sync", "PUT", body={"displayName": "master-change"}, realm="master")
        self.assertIn(status, (401, 403))

    def test_removing_manage_realm_denies_smtp_and_unrelated_updates(self):
        role = self.admin("GET", "/clients/" + self.management + "/roles/manage-realm")
        self.admin("DELETE", "/users/" + self.smtp_subject + "/role-mappings/clients/" + self.management, [role])
        for body in ({"smtpServer": dict(SMTP, host="denied.invalid")}, {"registrationAllowed": True}):
            self.expect(403, "smtp-sync", "PUT", body=body)
        status, _ = call(self.base, "PUT", "/admin/realms/" + self.realm, {"smtpServer": SMTP})
        self.assertEqual(401, status)
        self.assertEqual(SMTP, self.admin("GET", "")["smtpServer"])
        self.assertFalse(self.admin("GET", "")["registrationAllowed"])

    def test_fgap_rejects_realm_and_smtp_resource_permissions(self):
        authz = self.authz_path()
        self.expect(201, "smtp-sync", "POST", authz + "/policy/user", {
            "name": "smtp-subject", "logic": "POSITIVE", "users": [self.smtp_subject],
        })
        for resource_type in ("Realms", "SMTP"):
            with self.subTest(resource_type=resource_type):
                self.expect(400, "smtp-sync", "POST", authz + "/permission/scope", {
                    "name": "smtp-only-" + resource_type, "resourceType": resource_type,
                    "scopes": ["manage"], "policies": ["smtp-subject"],
                })

    def test_manage_realm_can_change_fgap_and_then_modify_users(self):
        self.expect(403, "smtp-sync", "PUT", "/users/" + self.human, {"firstName": "before-policy"})
        # Under C's enabled FGAP, manage-realm also administers the native
        # policy store. A known resource-server UUID is not a secret boundary.
        authz = self.authz_path()
        self.expect(201, "smtp-sync", "POST", authz + "/policy/user", {
            "name": "smtp-subject", "logic": "POSITIVE", "users": [self.smtp_subject],
        })
        self.expect(201, "smtp-sync", "POST", authz + "/permission/scope", {
            "name": "smtp-self-user-management", "resourceType": "Users",
            "scopes": ["manage"], "policies": ["smtp-subject"],
        })
        self.expect(204, "smtp-sync", "PUT", "/users/" + self.human, {"firstName": "policy-escalation"})
        self.assertEqual("policy-escalation", self.admin("GET", "/users/" + self.human)["firstName"])
        self.expect(403, "mail", "PUT", "/users/" + self.human, {"firstName": "mail-denied"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
