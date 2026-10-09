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
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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
                form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                owner.requests.append(("POST", self.path, dict(self.headers), form))
                if self.path == "/auth/realms/example/protocol/openid-connect/token":
                    if form != {"grant_type": ["client_credentials"], "client_id": ["configured-app-client"],
                                "client_secret": ["technical-client-secret-canary"]}:
                        return self.reply(401, {"error": "private-token-error"})
                    return self.reply(owner.technical_status, {"access_token": jwt(owner.claims)})
                if self.path == "/auth/realms/master/protocol/openid-connect/token":
                    if owner.admin_redirect:
                        self.send_response(302)
                        self.send_header("Location", "/credential-leak-target")
                        self.end_headers()
                        return
                    if form != {"grant_type": ["password"], "client_id": ["admin-cli"],
                                "username": ["realm-admin-canary"], "password": ["admin-password-canary"]}:
                        return self.reply(401)
                    return self.reply(owner.admin_status, {"access_token": "admin-token-canary"})
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
                if self.path != "/auth/admin/realms/example" or self.headers.get("Authorization") != "Bearer admin-token-canary":
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
            "KEYCLOAK_ADMIN_USERNAME": "realm-admin-canary", "KEYCLOAK_ADMIN_PASSWORD": "admin-password-canary",
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
                        "admin-password-canary", "admin-token-canary",
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


class TriggerSmtpTest(SmtpFixture):
    def setUp(self):
        super().setUp()
        port_source = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        port = port_source.server_port
        port_source.server_close()
        self.bridge_url = "http://127.0.0.1:" + str(port)
        self.process = subprocess.Popen(
            [sys.executable, "-B", str(SCRIPT), "--serve"],
            env={**self.env, "SMTP_RECONCILE_PORT": str(port)}, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.process.poll() is None:
            try:
                with urlopen(self.bridge_url + "/health", timeout=0.2) as response:
                    if response.status == 200:
                        return
            except (URLError, OSError):
                time.sleep(0.02)
        if self.process.poll() is None:
            self.process.terminate()
        output = self.process.communicate(timeout=2)
        super().tearDown()
        self.fail("bridge did not start: " + "".join(output))

    def tearDown(self):
        self.update_release.set()
        if hasattr(self, "process"):
            if self.process.poll() is None:
                self.process.terminate()
            output = "".join(self.process.communicate(timeout=12))
            for private in ("saved-password", "saved-username", "admin-password-canary",
                            "technical-password-canary", "private-smtp-provider-error"):
                self.assertNotIn(private, output)
            self.assertNotIn("Traceback", output)
        super().tearDown()

    def trigger(self, revision=1, token=None, body=None):
        request = Request(self.bridge_url + "/smtp/reconcile", method="POST",
                          data=json.dumps({"revision": revision} if body is None else body).encode(),
                          headers={"Content-Type": "application/json",
                                   "Authorization": "Bearer " + (jwt(self.claims) if token is None else token)})
        try:
            response = urlopen(request, timeout=8)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def test_authenticated_save_applies_one_current_snapshot_and_revision(self):
        self.assertEqual(self.trigger(), (200, {"appliedRevision": 1, "status": "APPLIED"}))
        self.assertEqual(len([r for r in self.requests if r[0] == "GET"]), 1)
        self.assertEqual(self.updates[-1]["smtpServer"]["password"], self.snapshot["globalSmtpPassword"])

    def test_forged_matching_claims_are_not_authority(self):
        self.assertEqual(self.trigger()[0], 200)
        self.requests.clear()
        self.updates.clear()
        forged = jwt(self.claims).rsplit(".", 1)[0] + ".forged-signature"
        self.assertEqual(self.trigger(token=forged)[0], 403)
        self.assertEqual(self.updates, [])
        self.assertFalse(any(r[1].startswith("/auth/") for r in self.requests))

    def test_install_hook_calls_same_bridge_without_realm_admin_secret(self):
        env = {key: value for key, value in self.env.items() if not key.startswith("KEYCLOAK_ADMIN_")}
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--trigger"],
                                env={**env, "SMTP_RECONCILE_URL": self.bridge_url + "/smtp/reconcile"},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SMTP_RECONCILE_APPLIED", result.stdout)
        self.assertEqual(len(self.updates), 1)
        self.assertTrue(any(r[1] == "/auth/realms/example/protocol/openid-connect/token" for r in self.requests))
        self.assertNotIn("password-canary", result.stdout + result.stderr)

    def test_install_trigger_recovers_from_busy_bridge_without_reapplying(self):
        self.update_release.clear()
        first = []
        first_thread = threading.Thread(target=lambda: first.append(self.trigger()))
        first_thread.start()
        self.assertTrue(self.update_started.wait(3))
        self.assertEqual(self.trigger()[0], 503)
        hook = subprocess.Popen(
            [sys.executable, "-B", str(SCRIPT), "--trigger"],
            env={**self.env, "SMTP_RECONCILE_URL": self.bridge_url + "/smtp/reconcile"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            time.sleep(0.4)  # The first POST observes the busy helper before it is released.
        finally:
            self.update_release.set()
        stdout, stderr = hook.communicate(timeout=12)
        first_thread.join(3)
        self.assertEqual(first, [(200, {"appliedRevision": 1, "status": "APPLIED"})])
        self.assertEqual(hook.returncode, 0, stderr)
        self.assertIn("SMTP_RECONCILE_APPLIED", stdout)
        self.assertNotIn("password-canary", stdout + stderr)
        self.assertEqual(len(self.updates), 1)

    def test_install_trigger_retries_connection_refusal_then_acknowledges(self):
        unused = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        port = unused.server_port
        unused.server_close()
        calls = []

        class LateBridge(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                calls.append((self.path, self.headers.get("Authorization")))
                body = self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200 if body == b'{"revision":0}' else 400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"appliedRevision":1,"status":"APPLIED"}')

        bridge = {}
        def start_bridge():
            time.sleep(0.25)
            bridge["server"] = ThreadingHTTPServer(("127.0.0.1", port), LateBridge)
            bridge["server"].serve_forever()

        thread = threading.Thread(target=start_bridge, daemon=True)
        thread.start()
        transient_failures = []
        original_request = reconciler.HttpClient.request

        def record_request(client, *args, **kwargs):
            try:
                return original_request(client, *args, **kwargs)
            except reconciler.TriggerNotReady:
                transient_failures.append(1)
                raise

        try:
            with patch.object(reconciler.HttpClient, "request", record_request):
                reconciler.trigger({**self.env, "SMTP_RECONCILE_URL": f"http://127.0.0.1:{port}/smtp/reconcile"},
                                   ready_seconds=2, retry_seconds=0.05)
            self.assertGreaterEqual(len(transient_failures), 1)
            self.assertGreaterEqual(len(calls), 1)
            self.assertEqual(calls[0][0], "/smtp/reconcile")
            self.assertTrue(calls[0][1].startswith("Bearer "))
        finally:
            if "server" in bridge:
                bridge["server"].shutdown()
                bridge["server"].server_close()
            thread.join(2)

    def test_install_trigger_exhausts_connection_budget_safely(self):
        unused = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        port = unused.server_port
        unused.server_close()
        with self.assertRaisesRegex(reconciler.ReconcileError, "SMTP_RECONCILE_TRIGGER_NOT_READY"):
            reconciler.trigger({**self.env, "SMTP_RECONCILE_URL": f"http://127.0.0.1:{port}/smtp/reconcile"},
                               ready_seconds=0.1, retry_seconds=0.02)
        self.assertEqual(self.updates, [])

    def test_install_trigger_waits_for_the_source_to_finish_starting(self):
        # Helm runs this hook without waiting for ConsultingTypeService to become
        # Ready, so the helper answers 502 SOURCE_UNAVAILABLE while that
        # Deployment is still rolling. That is as transient as the helper's own
        # 503 and must be waited out, not reported as a failed release.
        attempts = []

        class StartingBridge(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                attempts.append(1)
                if len(attempts) < 3:
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"code":"SMTP_RECONCILE_SOURCE_UNAVAILABLE"}')
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"appliedRevision":4,"status":"APPLIED"}')

        bridge = ThreadingHTTPServer(("127.0.0.1", 0), StartingBridge)
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        try:
            reconciler.trigger({**self.env, "SMTP_RECONCILE_URL":
                                f"http://127.0.0.1:{bridge.server_port}/smtp/reconcile"},
                               ready_seconds=2, retry_seconds=0.02)
            # It got past the 502s instead of failing on the first one; the
            # exact attempt count depends on retry timing.
            self.assertGreaterEqual(len(attempts), 3)
        finally:
            bridge.shutdown()
            bridge.server_close()
            thread.join(2)

    def test_install_trigger_does_not_retry_auth_or_permanent_helper_failure(self):
        failures = []

        class RefusingBridge(BaseHTTPRequestHandler):
            status = 403

            def log_message(self, *_):
                pass

            def do_POST(self):
                failures.append(self.status)
                self.send_response(self.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"code":"fixed-error"}')

        bridge = ThreadingHTTPServer(("127.0.0.1", 0), RefusingBridge)
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        try:
            for status in (401, 403, 500):
                with self.subTest(status=status):
                    RefusingBridge.status = status
                    failures.clear()
                    with self.assertRaises(reconciler.ReconcileError):
                        reconciler.trigger({**self.env, "SMTP_RECONCILE_URL":
                                            f"http://127.0.0.1:{bridge.server_port}/smtp/reconcile"},
                                           ready_seconds=0.5, retry_seconds=0.02)
                    self.assertEqual(failures, [status])
        finally:
            bridge.shutdown()
            bridge.server_close()
            thread.join(2)

    def test_absent_saved_snapshot_acknowledges_zero_and_clears_stale_transport(self):
        self.source_status = 204
        self.revision = 0
        self.assertEqual(self.trigger(0), (200, {"appliedRevision": 0, "status": "DISABLED_OR_INCOMPLETE"}))
        self.assertEqual(self.updates, [{"smtpServer": {}}])

    def test_wrong_subject_client_role_or_expiry_rejected_before_source(self):
        for field, value in (("sub", "other"), ("azp", "other"),
                             ("realm_access", {"roles": []}), ("exp", time.time() - 1)):
            with self.subTest(field=field):
                self.assertEqual(self.trigger(token=jwt({**self.claims, field: value}))[0], 403)
        self.assertEqual(self.requests, [])

    def test_replayed_old_trigger_reads_latest_and_does_not_regress_or_repeat_put(self):
        self.revision = 2
        self.snapshot["globalSmtpPassword"] = "rotated-latest-password"
        self.assertEqual(self.trigger(1), (200, {"appliedRevision": 2, "status": "APPLIED"}))
        self.assertEqual(self.trigger(1), (200, {"appliedRevision": 2, "status": "APPLIED"}))
        self.assertEqual(len(self.updates), 1)
        self.assertEqual(self.updates[0]["smtpServer"]["password"], "rotated-latest-password")

    def test_missing_revision_or_future_request_does_not_modify_realm(self):
        self.assertEqual(self.trigger(2)[0], 409)
        self.revision_header = False
        self.assertEqual(self.trigger()[0], 502)
        self.assertEqual(self.updates, [])

    def test_partial_or_disabled_source_clears_and_acknowledges_actual_revision(self):
        self.snapshot.pop("globalSmtpPassword")
        self.assertEqual(self.trigger(), (200, {"appliedRevision": 1, "status": "DISABLED_OR_INCOMPLETE"}))
        self.assertEqual(self.updates, [{"smtpServer": {}}])

    def test_source_outage_preserves_previous_realm_and_never_acknowledges(self):
        self.source_status = 503
        status, body = self.trigger()
        self.assertEqual(status, 502)
        self.assertNotIn("appliedRevision", body)
        self.assertEqual(self.updates, [])

    def test_failed_update_keeps_retry_eligible(self):
        self.update_status = 500
        self.assertEqual(self.trigger()[0], 502)
        self.update_status = 204
        self.assertEqual(self.trigger()[0], 200)
        self.assertEqual(len(self.updates), 1)

    def test_concurrent_save_is_busy_then_next_retry_applies_newest_snapshot(self):
        self.update_release.clear()
        first = []
        thread = threading.Thread(target=lambda: first.append(self.trigger()))
        thread.start()
        self.assertTrue(self.update_started.wait(3))
        self.revision = 2
        self.snapshot["globalSmtpPassword"] = "rotation-during-apply"
        self.assertEqual(self.trigger(2)[0], 503)
        self.update_release.set()
        thread.join(5)
        self.assertEqual(first, [(200, {"appliedRevision": 1, "status": "APPLIED"})])
        self.assertEqual(self.trigger(2), (200, {"appliedRevision": 2, "status": "APPLIED"}))
        self.assertEqual(self.updates[-1]["smtpServer"]["password"], "rotation-during-apply")

    def test_shutdown_drains_inflight_apply_before_process_exit(self):
        self.update_release.clear()
        result = []
        thread = threading.Thread(target=lambda: result.append(self.trigger()))
        thread.start()
        self.assertTrue(self.update_started.wait(3))
        self.process.terminate()
        time.sleep(0.1)
        self.assertIsNone(self.process.poll())
        self.update_release.set()
        thread.join(5)
        self.process.wait(5)
        self.assertEqual(result[0][0], 200)
        self.assertEqual(len(self.updates), 1)

    def test_request_accepts_only_revision_metadata(self):
        for body in ({"revision": True}, {"revision": -1}, {"revision": 2**63},
                     {"revision": 1, "smtpPassword": "must-not-be-accepted"}, {}):
            with self.subTest(body=body):
                self.assertEqual(self.trigger(body=body)[0], 400)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
