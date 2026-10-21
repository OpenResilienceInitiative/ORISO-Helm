"""Public process/HTTP contract; local APIs are synthetic, not native acceptance."""

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

SCRIPT = Path(__file__).resolve().parents[1] / "files/keycloak-reconcile-smtp.py"
KEY = bytes(range(32))
AUDIENCES = ["tenantservice", "agencyservice", "consultingtypeservice", "userservice",
             "oriso-task-commands"]
ROLES = ["smtp-sync", "system-email-delivery", "notification-dispatch", "notifications-technical"]


def encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class ScenarioCTransportTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "globalFeatureSystemNotificationEmailsEnabled": True,
            "globalSmtpEnabled": True, "globalSmtpHost": "smtp.example.test",
            "globalSmtpPort": "587", "globalSmtpSecure": False,
            "globalSmtpFrom": "ORISO <mail@example.test>",
            "globalSmtpUsername": "synthetic-user", "globalSmtpPassword": "synthetic-secret",
            "unrelatedSavedSetting": "must-not-enter-command",
        }
        self.revision = 7
        self.acks, self.puts, self.proofs, self.requests = [], [], [], []
        self.variant = "normal"
        self.audiences = list(AUDIENCES)
        self.roles = list(ROLES)
        self.reply_revision = 7
        self.reply_status = "APPLIED"
        self.ack_status = 204
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, code, body=None, revision=False):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                if revision:
                    self.send_header("X-Smtp-Revision", str(owner.revision))
                self.end_headers()
                if body is not None:
                    self.wfile.write(json.dumps(body).encode())

            def authenticated(self):
                return self.headers.get("Authorization") == "Bearer " + owner.token

            def do_POST(self):
                owner.requests.append(("POST", self.path))
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/auth/realms/example/protocol/openid-connect/token":
                    form = parse_qs(raw.decode())
                    if form != {"grant_type": ["client_credentials"], "client_id": ["backend-mail"],
                                "client_secret": ["synthetic-mail-client-secret"]}:
                        return self.reply(403)
                    return self.reply(200, {"access_token": owner.token})
                if self.path == "/cts/settingsadmin/smtp-sync-acknowledgement" and self.authenticated():
                    owner.acks.append(json.loads(raw))
                    return self.reply(owner.ack_status)
                self.reply(403)

            def do_GET(self):
                owner.requests.append(("GET", self.path))
                if self.path == "/cts/settingsadmin/smtp-credentials" and self.authenticated():
                    return self.reply(200, owner.snapshot, revision=True)
                # A misleading equal revision cannot establish the unseen body.
                if self.path.endswith("/oriso-smtp-control/v1/smtp"):
                    return self.reply(200, {"revision": owner.revision, "status": "APPLIED"})
                self.reply(403)

            def do_PUT(self):
                owner.requests.append(("PUT", self.path))
                if (self.path != "/auth/realms/example/oriso-smtp-control/v1/smtp"
                        or not self.authenticated()):
                    return self.reply(403)
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                body = json.loads(raw)
                proof = self.headers.get("X-ORISO-Task-Context", "")
                try:
                    payload, signature = proof.split(".")
                    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
                    signature = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
                    expected = hmac.new(KEY, ("proof\n" + payload).encode(), hashlib.sha256).digest()
                    digest = hmac.new(KEY, b"payload\n" + json.dumps(
                        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(),
                        hashlib.sha256).hexdigest()
                    valid = (hmac.compare_digest(signature, expected)
                             and claims["aud"] == "oriso-smtp-control"
                             and claims["target"] == "actual-internal-realm-id"
                             and claims["taskClient"] == "backend-mail"
                             and claims["taskSubject"] == "actual-mail-subject"
                             and claims["payloadDigest"] == digest)
                except Exception:
                    valid = False
                if not valid:
                    return self.reply(403)
                owner.puts.append(body)
                owner.proofs.append(claims)
                if owner.variant in ("lost-first", "lost-conflict") and len(owner.puts) == 1:
                    self.connection.shutdown(2)
                    self.connection.close()
                    return
                if owner.variant in ("conflict", "lost-conflict"):
                    return self.reply(409)
                self.reply(200, {"revision": owner.reply_revision, "status": owner.reply_status})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.issuer = self.base + "/auth/realms/example"
        self.env = {
            **os.environ, "TASK_IDENTITY_MODEL": "FIVE_ACTORS", "POD_NAMESPACE": "test",
            "KEYCLOAK_URL": self.base + "/auth", "KEYCLOAK_REALM": "example",
            "TASK_IDENTITY_ISSUER": self.issuer,
            "KEYCLOAK_REALM_INTERNAL_ID": "actual-internal-realm-id",
            "CONSULTING_TYPE_SERVICE_URL": self.base + "/cts",
            "IDENTITY_MAIL_CLIENT_ID": "backend-mail",
            "IDENTITY_MAIL_CLIENT_SECRET": "synthetic-mail-client-secret",
            "IDENTITY_MAIL_SERVICE_SUBJECT": "actual-mail-subject",
            "ORISO_TASK_CONTEXT_KEY": base64.b64encode(KEY).decode(),
        }
        for name in ("TECHNICAL_CLIENT_ID", "TECHNICAL_CLIENT_SECRET", "TECHNICAL_SERVICE_SUBJECT",
                     "SMTP_SYNC_CLIENT_ID", "SMTP_SYNC_CLIENT_SECRET"):
            self.env.pop(name, None)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def run_transport(self):
        claims = {"iss": self.issuer, "azp": "backend-mail",
                  "sub": "actual-mail-subject", "exp": int(time.time()) + 300,
                  "aud": self.audiences, "realm_access": {"roles": self.roles}}
        self.token = "e30." + encoded(json.dumps(claims).encode()) + ".synthetic-signature"
        return subprocess.run([sys.executable, str(SCRIPT)], env=self.env, capture_output=True,
                              text=True, timeout=15)

    def test_one_mail_credential_reads_saved_snapshot_applies_bounded_command_and_acks_exact_revision(self):
        result = self.run_transport()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([{"revision": 7, "status": "APPLIED"}], self.acks)
        self.assertEqual(1, len(self.puts))
        self.assertEqual(7, self.puts[0]["revision"])
        self.assertNotIn("unrelatedSavedSetting", self.puts[0])
        self.assertFalse(any("/admin/" in path for _, path in self.requests))
        self.assertNotIn("synthetic-secret", result.stdout + result.stderr)

    def test_lost_reply_retries_identical_snapshot_with_fresh_context_before_ack(self):
        self.variant = "lost-first"
        result = self.run_transport()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(2, len(self.puts))
        self.assertEqual(self.puts[0], self.puts[1])
        self.assertNotEqual(self.proofs[0]["jti"], self.proofs[1]["jti"])
        self.assertNotEqual(self.proofs[0]["nonce"], self.proofs[1]["nonce"])
        self.assertEqual([{"revision": 7, "status": "APPLIED"}], self.acks)
        self.assertFalse(any(method == "GET" and "oriso-smtp-control" in path
                             for method, path in self.requests))

    def test_lost_conflicting_put_cannot_ack_from_matching_get_revision(self):
        self.variant = "lost-conflict"
        result = self.run_transport()
        self.assertEqual(2, result.returncode)
        self.assertEqual(2, len(self.puts))
        self.assertEqual([], self.acks)
        self.assertFalse(any(method == "GET" and "oriso-smtp-control" in path
                             for method, path in self.requests))

    def test_known_conflict_is_not_retried_or_acknowledged(self):
        self.variant = "conflict"
        result = self.run_transport()
        self.assertEqual(2, result.returncode)
        self.assertEqual(1, len(self.puts))
        self.assertEqual([], self.acks)

    def test_wrong_revision_or_status_response_stays_pending(self):
        for revision, status in ((8, "APPLIED"), (7, "NOT_APPLIED"), (7.0, "APPLIED")):
            with self.subTest(revision=revision, status=status):
                self.reply_revision, self.reply_status = revision, status
                result = self.run_transport()
                self.assertEqual(2, result.returncode)
                self.assertEqual([], self.acks)

    def test_noncanonical_bearer_audiences_or_roles_stop_before_saved_secret_read(self):
        for audiences, roles in ((AUDIENCES[:-1], ROLES),
                                 (AUDIENCES + ["oriso-smtp-control"], ROLES),
                                 (AUDIENCES, ROLES + ["technical"])):
            with self.subTest(audiences=audiences, roles=roles):
                self.audiences, self.roles = audiences, roles
                self.requests.clear()
                result = self.run_transport()
                self.assertEqual(2, result.returncode)
                self.assertEqual([], self.acks)
                self.assertEqual(1, len(self.requests))

    def test_missing_actual_realm_or_invalid_context_key_refuses_before_http(self):
        cases = (("KEYCLOAK_REALM_INTERNAL_ID", ""), ("ORISO_TASK_CONTEXT_KEY", "not-base64"),
                 ("ORISO_TASK_CONTEXT_KEY", base64.b64encode(b"short").decode()),
                 ("ORISO_TASK_CONTEXT_KEY", base64.b64encode(
                     b"same-synthetic-secret-with-32-bytes").decode()))
        original = dict(self.env)
        for name, value in cases:
            with self.subTest(name=name, value=value):
                self.env = dict(original)
                self.env[name] = value
                if name == "ORISO_TASK_CONTEXT_KEY" and value == cases[-1][1]:
                    self.env["IDENTITY_MAIL_CLIENT_SECRET"] = "same-synthetic-secret-with-32-bytes"
                self.requests.clear()
                result = self.run_transport()
                self.assertEqual(2, result.returncode)
                self.assertEqual([], self.requests)

    def test_disabled_saved_configuration_applies_disabled_then_acks_same_revision(self):
        self.snapshot["globalSmtpEnabled"] = False
        self.reply_status = "DISABLED_OR_INCOMPLETE"
        result = self.run_transport()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([{"revision": 7, "status": "DISABLED_OR_INCOMPLETE"}], self.acks)

    def test_opaque_nonempty_password_is_never_trimmed_or_reclassified(self):
        self.snapshot["globalSmtpPassword"] = "    "
        result = self.run_transport()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("    ", self.puts[0]["globalSmtpPassword"])
        self.assertEqual([{"revision": 7, "status": "APPLIED"}], self.acks)

    def test_missing_ack_endpoint_is_a_failed_c_run_even_after_known_smtp_write(self):
        self.ack_status = 404
        result = self.run_transport()
        self.assertEqual(2, result.returncode)
        self.assertEqual(1, len(self.puts))

    def test_pinned_public_issuer_is_independent_of_internal_keycloak_transport_url(self):
        self.issuer = "https://identity.example.test/auth/realms/example"
        self.env["TASK_IDENTITY_ISSUER"] = self.issuer
        result = self.run_transport()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([{"revision": 7, "status": "APPLIED"}], self.acks)


if __name__ == "__main__":
    unittest.main()
