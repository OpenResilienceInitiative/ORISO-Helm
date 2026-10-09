"""Exercise the actual reconciler process against local authenticated HTTP APIs."""

import base64
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "files" / "keycloak-reconcile-smtp.py"

spec = importlib.util.spec_from_file_location("keycloak_reconcile_smtp", SCRIPT)
reconciler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reconciler)


def jwt(claims):
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "e30." + encoded + ".fake-test-signature"


class SmtpFixture(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "globalFeatureSystemNotificationEmailsEnabled": True,
            "globalSmtpEnabled": True,
            "globalSmtpHost": "smtp.provider.example",
            "globalSmtpPort": "587",
            "globalSmtpSecure": False,
            "globalSmtpFrom": "Configured Platform <sender@provider.example>",
            "globalSmtpUsername": "saved-username",
            "globalSmtpPassword": 'saved-password"\\value',
        }
        self.claims = {
            "sub": "environment-technical-subject", "azp": "configured-app-client",
            "realm_access": {"roles": ["technical"]}, "exp": time.time() + 300,
        }
        self.sync_claims = {
            "sub": "sync-service-account", "azp": "smtp-sync", "exp": time.time() + 300,
            "resource_access": {"realm-management": {"roles": ["manage-realm"]}},
        }
        self.requests = []
        self.updates = []
        self.source_status = 200
        self.technical_status = 200
        self.admin_status = 200
        self.update_status = 204
        self.raw_source_body = None
        self.source_redirect = False
        self.admin_redirect = False
        self.revision = 1
        self.revision_header = True
        self.acks = []
        self.ack_status = 204
        self.update_started = threading.Event()
        self.update_release = threading.Event()
        self.update_release.set()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, body=None, revision=None):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if revision is not None and owner.revision_header:
                    self.send_header("X-Smtp-Revision", str(revision))
                self.end_headers()
                if body is not None:
                    self.wfile.write(json.dumps(body).encode())

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/cts/settingsadmin/smtp-sync-acknowledgement":
                    owner.requests.append(("POST", self.path, dict(self.headers), json.loads(raw)))
                    if (self.headers.get("Authorization") != "Bearer " + jwt(owner.claims)
                            or self.headers.get("tenantId") != "0"):
                        return self.reply(403)
                    owner.acks.append(json.loads(raw))
                    return self.reply(owner.ack_status)
                form = parse_qs(raw.decode())
                owner.requests.append(("POST", self.path, dict(self.headers), form))
                if self.path == "/auth/realms/example/protocol/openid-connect/token":
                    if form == {"grant_type": ["client_credentials"], "client_id": ["smtp-sync"],
                                "client_secret": ["sync-client-secret-canary"]}:
                        if owner.admin_redirect:
                            self.send_response(302)
                            self.send_header("Location", "/credential-leak-target")
                            self.end_headers()
                            return
                        return self.reply(owner.admin_status, {"access_token": jwt(owner.sync_claims)})
                    if form != {"grant_type": ["client_credentials"], "client_id": ["configured-app-client"],
                                "client_secret": ["technical-client-secret-canary"]}:
                        return self.reply(401, {"error": "private-token-error"})
                    return self.reply(owner.technical_status, {"access_token": jwt(owner.claims)})
                # The master realm is never a valid authority for this helper.
                self.reply(404)

            def do_GET(self):
                owner.requests.append(("GET", self.path, dict(self.headers), None))
                if self.path != "/cts/settingsadmin/smtp-credentials":
                    return self.reply(404)
                if self.headers.get("Authorization") != "Bearer " + jwt(owner.claims):
                    return self.reply(403)
                if self.headers.get("tenantId") != "0":
                    return self.reply(403)
                if owner.source_redirect:
                    self.send_response(302)
                    self.send_header("Location", "/credential-leak-target")
                    self.end_headers()
                    return
                if owner.raw_source_body is not None:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(owner.raw_source_body)
                    return
                self.reply(owner.source_status, owner.snapshot, owner.revision)

            def do_PUT(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(("PUT", self.path, dict(self.headers), payload))
                if self.path != "/auth/admin/realms/example" or self.headers.get("Authorization") != "Bearer " + jwt(owner.sync_claims):
                    return self.reply(403)
                owner.update_started.set()
                owner.update_release.wait(10)
                if owner.update_status == 204:
                    owner.updates.append(payload)
                self.reply(owner.update_status, {"error": "private-smtp-provider-error"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        base = "http://127.0.0.1:" + str(self.server.server_port)
        self.env = {
            "PATH": os.environ["PATH"], "POD_NAMESPACE": "test", "KEYCLOAK_URL": base + "/auth",
            "KEYCLOAK_REALM": "example", "CONSULTING_TYPE_SERVICE_URL": base + "/cts",
            "TECHNICAL_CLIENT_SECRET": "technical-client-secret-canary",
            "TECHNICAL_SERVICE_SUBJECT": "environment-technical-subject",
            "TECHNICAL_CLIENT_ID": "configured-app-client",
            "SMTP_SYNC_CLIENT_ID": "smtp-sync", "SMTP_SYNC_CLIENT_SECRET": "sync-client-secret-canary",
        }

    def tearDown(self):
        self.update_release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def run_reconciler(self, **overrides):
        command = [sys.executable, "-B", str(SCRIPT)]
        result = subprocess.run(command, env={**self.env, **overrides}, capture_output=True, text=True, timeout=15)
        output = result.stdout + result.stderr
        for private in (self.env["TECHNICAL_CLIENT_SECRET"], "technical-password-canary",
                        "sync-client-secret-canary", jwt(self.sync_claims),
                        "private-smtp-provider-error", "private-token-error", "saved-username"):
            self.assertNotIn(private, output)
        self.assertNotIn("Traceback", output)
        return result


class ReconcileSmtpTest(SmtpFixture):
    def test_saved_snapshot_updates_starttls_without_chart_values(self):
        result = self.run_reconciler()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len([r for r in self.requests if r[0] == "GET"]), 1)
        smtp = self.updates[0]["smtpServer"]
        self.assertEqual(smtp["host"], "smtp.provider.example")
        self.assertEqual(smtp["from"], "sender@provider.example")
        self.assertEqual(smtp["fromDisplayName"], "Configured Platform")
        self.assertEqual(smtp["ssl"], "false")
        self.assertEqual(smtp["starttls"], "true")
        self.assertEqual(smtp["password"], self.snapshot["globalSmtpPassword"])
        self.assertNotIn(self.snapshot["globalSmtpPassword"], result.stdout + result.stderr)

    def test_password_and_transport_rotation_follow_new_saved_snapshot(self):
        self.assertEqual(self.run_reconciler().returncode, 0)
        self.snapshot.update(globalSmtpPassword=" rotated-password-canary ", globalSmtpPort="465", globalSmtpSecure=True)
        self.assertEqual(self.run_reconciler().returncode, 0)
        self.assertEqual(len(self.updates), 2)
        self.assertEqual(self.updates[-1]["smtpServer"]["password"], " rotated-password-canary ")
        self.assertEqual(self.updates[-1]["smtpServer"]["ssl"], "true")
        self.assertEqual(self.updates[-1]["smtpServer"]["starttls"], "false")

    def test_chart_transport_never_overrides_admin_snapshot(self):
        result = self.run_reconciler(SMTP_HOST="legacy.invalid", SMTP_PORT="465", SMTP_SECURE="true",
                                     SMTP_FROM="legacy@example.invalid", SMTP_USER="legacy-user", SMTP_PASSWORD="legacy-password")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.updates[0]["smtpServer"]["host"], self.snapshot["globalSmtpHost"])
        self.assertEqual(self.updates[0]["smtpServer"]["password"], self.snapshot["globalSmtpPassword"])

    def test_opaque_credentials_are_preserved_without_stripping_or_control_filtering(self):
        self.snapshot["globalSmtpPassword"] = ' \tunicode-credential-ä"\\\n '
        self.snapshot["globalSmtpUsername"] = " opaque-username-canary "
        result = self.run_reconciler()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.updates[-1]["smtpServer"].get("password"), self.snapshot["globalSmtpPassword"])
        self.assertEqual(self.updates[-1]["smtpServer"].get("user"), self.snapshot["globalSmtpUsername"])
        self.assertNotIn(self.snapshot["globalSmtpPassword"], result.stdout + result.stderr)

    def test_disabled_saved_switches_clear_old_realm_transport(self):
        for field in ("globalFeatureSystemNotificationEmailsEnabled", "globalSmtpEnabled"):
            with self.subTest(field=field):
                self.snapshot[field] = False
                result = self.run_reconciler()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.updates[-1], {"smtpServer": {}})
                self.assertIn("SMTP_DISABLED_OR_INCOMPLETE", result.stdout)
                self.snapshot[field] = True

    def test_partial_snapshot_clears_old_realm_transport_without_fallback(self):
        for field in ("globalSmtpHost", "globalSmtpPort", "globalSmtpFrom", "globalSmtpUsername", "globalSmtpPassword", "globalSmtpSecure"):
            with self.subTest(field=field):
                value = self.snapshot.pop(field)
                result = self.run_reconciler()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.updates[-1], {"smtpServer": {}})
                self.snapshot[field] = value

    def test_absent_snapshot_clears_old_realm_transport(self):
        self.source_status = 204
        result = self.run_reconciler()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.updates, [{"smtpServer": {}}])

    def test_upstream_statuses_do_not_modify_previous_realm(self):
        for status in (401, 403, 500, 503):
            with self.subTest(status=status):
                self.source_status = status
                self.snapshot = {"error": "private-smtp-provider-error"}
                result = self.run_reconciler()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SMTP_RECONCILE_SOURCE_UNAVAILABLE", result.stderr)
                self.assertEqual(self.updates, [])

    def test_wrong_subject_client_role_and_expired_token_never_read_credentials(self):
        original = dict(self.claims)
        for field, value in (("sub", "other-subject"), ("azp", "other-client"),
                             ("realm_access", {"roles": ["tenant-admin"]}), ("exp", time.time() - 60)):
            with self.subTest(field=field):
                self.claims = {**original, field: value}
                self.requests = []
                result = self.run_reconciler()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual([r for r in self.requests if r[0] != "POST"], [])
                self.assertEqual(self.updates, [])

    def test_technical_auth_failure_does_not_read_or_update_settings(self):
        self.technical_status = 401
        self.assertNotEqual(self.run_reconciler().returncode, 0)
        self.assertEqual([r for r in self.requests if r[0] != "POST"], [])
        self.assertEqual(self.updates, [])

    def test_admin_auth_or_update_failure_is_safe(self):
        self.admin_status = 401
        self.assertNotEqual(self.run_reconciler().returncode, 0)
        self.assertEqual(self.updates, [])
        self.admin_status = 200
        self.update_status = 500
        result = self.run_reconciler()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.updates, [])

    def test_invalid_json_and_nonobject_response_do_not_clear_existing_transport(self):
        for body in (b"not-json-with-private-provider-detail", b"[]", b"null"):
            with self.subTest(body=body):
                self.raw_source_body = body
                result = self.run_reconciler()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.updates, [])
                self.assertNotIn(body.decode(), result.stdout + result.stderr)

    def test_redirect_is_not_followed_with_technical_bearer_token(self):
        self.source_redirect = True
        self.assertNotEqual(self.run_reconciler().returncode, 0)
        self.assertFalse(any(r[1] == "/credential-leak-target" for r in self.requests))
        self.assertEqual(self.updates, [])

    def test_admin_redirect_is_classified_as_update_failure_without_following(self):
        self.admin_redirect = True
        result = self.run_reconciler()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SMTP_RECONCILE_KEYCLOAK_UPDATE_FAILED", result.stderr)
        self.assertFalse(any(r[1] == "/credential-leak-target" for r in self.requests))
        self.assertEqual(self.updates, [])

    def test_network_failure_never_clears_existing_transport(self):
        unused = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        port = unused.server_port
        unused.server_close()
        result = self.run_reconciler(CONSULTING_TYPE_SERVICE_URL="http://127.0.0.1:" + str(port))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SMTP_RECONCILE_SOURCE_UNAVAILABLE", result.stderr)
        self.assertEqual(self.updates, [])

    def test_invalid_saved_transport_is_disabled_without_printing_secrets(self):
        original = dict(self.snapshot)
        for field, value in (("globalSmtpPort", "0"), ("globalSmtpPort", "65536"),
                             ("globalSmtpPort", "not-a-port"), ("globalSmtpSecure", "false"),
                             ("globalSmtpFrom", "invalid"), ("globalSmtpFrom", "private\nvalue")):
            with self.subTest(field=field, value=value):
                self.snapshot = {**original, field: value}
                result = self.run_reconciler()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.updates[-1], {"smtpServer": {}})

    def test_public_http_and_credential_urls_are_rejected_before_login(self):
        for value in ("http://keycloak.public.example/auth", "https://user:private@auth.example",
                      "https://auth.example/auth?private=credential", "https://auth.example/auth#fragment"):
            with self.subTest(value=value):
                result = self.run_reconciler(KEYCLOAK_URL=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.requests, [])

    def test_tls_endpoint_never_downgrades_to_working_plaintext_endpoint(self):
        result = self.run_reconciler(KEYCLOAK_URL=self.env["KEYCLOAK_URL"].replace("http:", "https:"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SMTP_RECONCILE_SOURCE_UNAVAILABLE", result.stderr)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.updates, [])

    def test_missing_identity_configuration_never_guesses_an_account(self):
        for field in ("TECHNICAL_CLIENT_SECRET", "TECHNICAL_SERVICE_SUBJECT", "TECHNICAL_CLIENT_ID"):
            with self.subTest(field=field):
                result = self.run_reconciler(**{field: ""})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.requests, [])


    def test_writes_with_realm_sync_client_and_never_touches_master(self):
        result = self.run_reconciler()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.updates), 1)
        self.assertFalse(any("/realms/master/" in r[1] for r in self.requests))
        grants = [r[3] for r in self.requests if r[1].endswith("/openid-connect/token")]
        self.assertTrue(all(g["grant_type"] == ["client_credentials"] for g in grants))
        self.assertIn(["smtp-sync"], [g["client_id"] for g in grants])

    def test_master_admin_credentials_are_neither_required_nor_used(self):
        result = self.run_reconciler(KEYCLOAK_ADMIN_USERNAME="", KEYCLOAK_ADMIN_PASSWORD="")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_sync_client_configuration_fails_before_any_request(self):
        for field in ("SMTP_SYNC_CLIENT_ID", "SMTP_SYNC_CLIENT_SECRET"):
            with self.subTest(field=field):
                result = self.run_reconciler(**{field: ""})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.requests, [])

    def test_master_realm_is_refused_before_any_request(self):
        result = self.run_reconciler(KEYCLOAK_REALM="master")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.requests, [])

    def test_wrong_sync_identity_never_updates_realm(self):
        original = dict(self.sync_claims)
        for field, value in (("azp", "other-client"), ("resource_access", {}), ("exp", time.time() - 60)):
            with self.subTest(field=field):
                self.sync_claims = {**original, field: value}
                result = self.run_reconciler()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SMTP_RECONCILE_SYNC_IDENTITY_MISMATCH", result.stderr)
                self.assertFalse(any(r[0] == "PUT" for r in self.requests))
        self.sync_claims = original


    # CTS shows the Admin panel status from this acknowledgement (Helm#420).
    def test_acknowledges_exactly_the_revision_it_wrote(self):
        self.revision = 41
        result = self.run_reconciler()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.acks, [{"revision": 41, "status": "APPLIED"}])
        put = next(i for i, r in enumerate(self.requests) if r[0] == "PUT")
        ack = next(i for i, r in enumerate(self.requests) if r[1].endswith("/smtp-sync-acknowledgement"))
        self.assertLess(put, ack)

    def test_disabled_snapshot_is_acknowledged_as_disabled(self):
        self.snapshot["globalSmtpEnabled"] = False
        self.assertEqual(self.run_reconciler().returncode, 0)
        self.assertEqual(self.acks, [{"revision": 1, "status": "DISABLED_OR_INCOMPLETE"}])

    def test_failed_keycloak_write_is_never_acknowledged(self):
        self.update_status = 500
        self.assertNotEqual(self.run_reconciler().returncode, 0)
        self.assertEqual(self.acks, [])

    def test_absent_settings_revision_zero_is_not_acknowledged(self):
        self.source_status = 204
        self.revision = 0
        self.assertEqual(self.run_reconciler().returncode, 0)
        self.assertEqual(self.acks, [])

    def test_missing_or_invalid_revision_fails_before_writing(self):
        for header, value in ((False, 1), (True, "not-a-number"), (True, -1)):
            with self.subTest(value=value, header=header):
                self.revision_header, self.revision = header, value
                result = self.run_reconciler()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SMTP_RECONCILE_SOURCE_REVISION_INVALID", result.stderr)
                self.assertEqual(self.updates, [])
                self.assertEqual(self.acks, [])

    def test_newer_save_conflict_is_left_for_the_next_run(self):
        self.ack_status = 409
        result = self.run_reconciler()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SMTP_RECONCILE_ACK_STALE", result.stdout)

    def test_acknowledgement_failure_is_visible(self):
        for status in (403, 500):
            with self.subTest(status=status):
                self.ack_status = status
                result = self.run_reconciler()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SMTP_RECONCILE_ACK_FAILED", result.stderr)

    def test_no_long_running_helper_modes_remain(self):
        for mode in ("--serve", "--trigger"):
            with self.subTest(mode=mode):
                result = subprocess.run([sys.executable, "-B", str(SCRIPT), mode], env=self.env,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 2)
                self.assertIn("SMTP_RECONCILE_CONFIGURATION_INVALID: mode", result.stderr)
                self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
