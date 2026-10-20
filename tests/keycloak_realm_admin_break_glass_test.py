#!/usr/bin/env python3
"""Real Keycloak seam for Helm#422.

Fresh import: the ORISO-realm realmadmin is disabled. Existing realm: the
reconcile Job leaves it alone by default, and with the opt-in flag disables it,
ends its sessions and stays idempotent.
Run: python3 tests/keycloak_realm_admin_break_glass_test.py -v
"""
import unittest

import _keycloak_container as kc

PASSWORD = "Synthetic-break-glass-credential-0001"


class RealmAdminBreakGlassTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keycloak = kc.KeycloakContainer("oriso-break-glass-fixture").start()
        cls.addClassCleanup(cls.keycloak.stop)
        cls.base = cls.keycloak.base
        cls.realm = kc.rendered_realm()

    def admin(self, method, path, body=None):
        return kc.request(self.base, method, "/admin/realms" + path, body, kc.master_admin(self.base), timeout=90)

    def realm_admin(self, realm):
        users = self.admin("GET", "/" + realm + "/users?exact=true&username=realmadmin")
        self.assertEqual(len(users), 1, users)
        return users[0]

    def person_login(self, realm):
        return kc.status_of(lambda: kc.request(self.base, "POST", "/realms/" + realm + "/protocol/openid-connect/token",
                                               {"grant_type": "password", "client_id": "admin-cli",
                                                "username": "realmadmin", "password": PASSWORD}, form=True))

    def reconcile(self, realm, subjects, **extra):
        env = kc.service_identity_env(self.base, realm, REALM_ADMIN_USERNAME="realmadmin", **subjects, **extra)
        return kc.run_script("keycloak-reconcile-service-clients.py", env)

    def test_fresh_import_disables_realmadmin(self):
        fresh = dict(self.realm, realm="bg-fresh")
        fresh.pop("id", None)
        self.admin("POST", "", fresh)
        self.assertIs(self.realm_admin("bg-fresh")["enabled"], False)

    def test_existing_realm_disables_realmadmin_only_on_opt_in(self):
        # Existing realm as it looks today: realmadmin enabled with a password and a live session.
        existing = kc.strip_ids(self.realm)
        existing["realm"] = "bg-existing"
        for user in existing["users"]:
            if user["username"] == "realmadmin":
                user.update(enabled=True, requiredActions=[], email="realmadmin@synthetic.example", emailVerified=True,
                            credentials=[{"type": "password", "value": PASSWORD, "temporary": False}])
        self.admin("POST", "", existing)
        self.assertEqual(self.person_login("bg-existing"), 200)
        uid = self.realm_admin("bg-existing")["id"]
        self.assertTrue(self.admin("GET", "/bg-existing/users/" + uid + "/sessions"))

        subjects = kc.prepared_subjects(self.base, "bg-existing")
        out = self.reconcile("bg-existing", subjects, PREPARE_ONLY="false", DISABLE_REALM_ADMIN="false")
        self.assertNotIn("REALM_ADMIN", out)
        self.assertIs(self.realm_admin("bg-existing")["enabled"], True)

        for _ in range(2):
            out = self.reconcile("bg-existing", subjects, PREPARE_ONLY="false", DISABLE_REALM_ADMIN="true")
            self.assertIn("REALM_ADMIN_DISABLED", out)
            self.assertIs(self.realm_admin("bg-existing")["enabled"], False)
            self.assertEqual(self.admin("GET", "/bg-existing/users/" + uid + "/sessions"), [])
            self.assertIn(self.person_login("bg-existing"), (400, 401))
        # Master bootstrap admin is untouched.
        self.assertTrue(kc.master_admin(self.base))


if __name__ == "__main__":
    unittest.main()
