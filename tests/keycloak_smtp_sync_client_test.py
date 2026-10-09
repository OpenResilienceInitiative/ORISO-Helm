#!/usr/bin/env python3
"""Real Keycloak seam for Helm#420.

Proves on a fresh and on an existing realm that the chart's own scripts give the
smtp-sync client exactly manage-realm, that this client (not master admin)
writes the SMTP settings, and that an unchanged snapshot is not written again.
Run: python3 tests/keycloak_smtp_sync_client_test.py -v
"""
from contextlib import contextmanager
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import _keycloak_container as kc

REVISION = 3
SNAPSHOT = {
    "globalFeatureSystemNotificationEmailsEnabled": True, "globalSmtpEnabled": True,
    "globalSmtpHost": "smtp.synthetic.example", "globalSmtpPort": "587", "globalSmtpSecure": False,
    "globalSmtpFrom": "Synthetic Sender <sender@synthetic.example>",
    "globalSmtpUsername": "synthetic-user", "globalSmtpPassword": "synthetic-smtp-password",
}


def without_sync_client(realm):
    realm = kc.strip_ids(realm)
    realm["clients"] = [c for c in realm["clients"] if c["clientId"] != "smtp-sync"]
    realm["users"] = [u for u in realm["users"] if u.get("serviceAccountClientId") != "smtp-sync"]
    realm["clientScopeMappings"]["realm-management"] = [
        m for m in realm["clientScopeMappings"]["realm-management"] if m["client"] != "smtp-sync"]
    return realm


@contextmanager
def fake_admin_settings(acks):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            ok = self.path == "/settingsadmin/smtp-credentials" and self.headers.get("Authorization", "").startswith("Bearer ")
            self.send_response(200 if ok else 403)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Smtp-Revision", str(REVISION))
            self.end_headers()
            if ok:
                self.wfile.write(json.dumps(SNAPSHOT).encode())

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            ok = self.path == "/settingsadmin/smtp-sync-acknowledgement"
            if ok:
                acks.append(body)
            self.send_response(204 if ok else 404)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()


class SmtpSyncClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keycloak = kc.KeycloakContainer("oriso-smtp-sync-fixture").start()
        cls.addClassCleanup(cls.keycloak.stop)
        cls.base = cls.keycloak.base
        cls.realm = kc.rendered_realm()

    def setUp(self):
        self.acks = []
        context = fake_admin_settings(self.acks)
        self.cts = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def import_realm(self, realm, name):
        realm = dict(realm, realm=name)
        realm.pop("id", None)
        kc.request(self.base, "POST", "/admin/realms", realm, kc.master_admin(self.base), timeout=90)

    def prove(self, realm_name, subjects):
        base = self.base
        common = {"KEYCLOAK_URL": base, "KEYCLOAK_REALM": realm_name, "POD_NAMESPACE": "fixture",
                  "TECHNICAL_CLIENT_ID": "backend-technical", "SMTP_SYNC_CLIENT_ID": "smtp-sync",
                  "CONSULTING_TYPE_SERVICE_URL": self.cts, **kc.SECRETS, **subjects}
        # Service-identity hook (the only step that still uses the master installer).
        out = kc.run_script("keycloak-reconcile-service-clients.py",
                            kc.service_identity_env(base, realm_name, PREPARE_ONLY="false", **subjects))
        self.assertIn("SMTP_SYNC_CLIENT_RECONCILED", out)
        # SMTP Job: no master credential in its environment at all.
        out = kc.run_script("keycloak-reconcile-smtp.py", common)
        self.assertIn("SMTP_RECONCILE_APPLIED", out)
        self.assertIn("SMTP_RECONCILE_ACKNOWLEDGED", out)
        self.assertEqual(self.acks, [{"revision": REVISION, "status": "APPLIED"}])
        smtp = kc.request(base, "GET", "/admin/realms/" + realm_name, token=kc.master_admin(base))["smtpServer"]
        self.assertEqual((smtp["host"], smtp["from"], smtp["fromDisplayName"], smtp["user"], smtp["starttls"],
                          smtp["port"]), ("smtp.synthetic.example", "sender@synthetic.example", "Synthetic Sender",
                                          "synthetic-user", "true", "587"))
        # Next CronJob run: Keycloak masks the password, the fingerprint still proves it unchanged.
        out = kc.run_script("keycloak-reconcile-smtp.py", common)
        self.assertIn("SMTP_RECONCILE_UNCHANGED", out)
        self.assertIn("SMTP_RECONCILE_ACKNOWLEDGED", out)
        # Narrowness: realm settings only, no users, no master realm.
        sync = kc.request(base, "POST", "/realms/" + realm_name + "/protocol/openid-connect/token",
                          {"grant_type": "client_credentials", "client_id": "smtp-sync",
                           "client_secret": kc.SECRETS["SMTP_SYNC_CLIENT_SECRET"]}, form=True)["access_token"]
        issued = kc.claims(sync)
        self.assertEqual(issued["resource_access"], {"realm-management": {"roles": ["manage-realm"]}})
        self.assertFalse(issued.get("realm_access", {}).get("roles"))
        self.assertEqual(kc.status_of(lambda: kc.request(base, "GET", "/admin/realms/" + realm_name + "/users",
                                                         token=sync)), 403)
        self.assertIn(kc.status_of(lambda: kc.request(base, "GET", "/admin/realms/master", token=sync)), (401, 403))
        master_login = kc.status_of(lambda: kc.request(
            base, "POST", "/realms/master/protocol/openid-connect/token",
            {"grant_type": "client_credentials", "client_id": "smtp-sync",
             "client_secret": kc.SECRETS["SMTP_SYNC_CLIENT_SECRET"]}, form=True))
        self.assertIn(master_login, (400, 401))
        master_clients = kc.request(base, "GET", "/admin/realms/master/clients?clientId=smtp-sync",
                                    token=kc.master_admin(base))
        self.assertEqual(master_clients, [], "smtp-sync must not exist in master")

    def test_fresh_realm_imports_smtp_sync_and_writes_smtp(self):
        self.import_realm(self.realm, "smtp-fresh")
        self.prove("smtp-fresh", kc.SUBJECTS)

    def test_existing_realm_gets_smtp_sync_from_the_service_identity_hook(self):
        self.import_realm(without_sync_client(self.realm), "smtp-existing")
        self.prove("smtp-existing", kc.prepared_subjects(self.base, "smtp-existing"))


if __name__ == "__main__":
    unittest.main()
