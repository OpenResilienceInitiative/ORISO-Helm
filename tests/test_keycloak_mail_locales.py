import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs

import yaml


REALM_PATH = (
    Path(__file__).resolve().parents[1]
    / "charts"
    / "keycloak"
    / "keycloak-resources"
    / "realm.json"
)
ROOT = Path(__file__).resolve().parents[1]
PYTHON_SCRIPT = ROOT / "files" / "keycloak-reconcile-mail-locales.py"
HTTP_HELPER = ROOT / "files" / "keycloak-reconcile-smtp.py"


class KeycloakMailLocalesTest(unittest.TestCase):
    def test_realm_readback_rejects_missing_extra_duplicate_or_mistyped_locales(self):
        expected = ["de", "en", "fr", "ru", "ti", "tr"]
        readback = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"access_token":"token-canary"}')

            def do_PUT(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(204)
                self.end_headers()

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(readback).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                (Path(directory) / "keycloak_reconcile_smtp.py").write_bytes(HTTP_HELPER.read_bytes())
                (Path(directory) / PYTHON_SCRIPT.name).write_bytes(PYTHON_SCRIPT.read_bytes())
                command = [sys.executable, str(Path(directory) / PYTHON_SCRIPT.name)]
                env = {**os.environ,
                    "KEYCLOAK_URL": f"http://127.0.0.1:{server.server_port}/auth",
                    "KEYCLOAK_REALM": "example", "POD_NAMESPACE": "test",
                    "KEYCLOAK_ADMIN_USERNAME": "admin-canary",
                    "KEYCLOAK_ADMIN_PASSWORD": "secret-canary"}
                cases = (
                    ("missing", expected[:-1], True),
                    ("extra", expected + ["es"], True),
                    ("duplicate", expected[:-1] + ["ti"], True),
                    ("wrong type", ",".join(expected), True),
                    ("non-boolean enabled", expected, 1),
                )
                for name, locales, enabled in cases:
                    with self.subTest(name=name):
                        readback = {"internationalizationEnabled": enabled,
                            "supportedLocales": locales, "defaultLocale": "de",
                            "emailTheme": "oriso"}
                        result = subprocess.run(command, env=env, capture_output=True, text=True)
                        self.assertEqual(2, result.returncode)
                        self.assertEqual("MAIL_LOCALE_REALM_READBACK_MISMATCH",
                                         result.stderr.strip())
                        self.assertNotIn("secret-canary", result.stdout + result.stderr)
        finally:
            server.shutdown()
            server.server_close()

    def test_transient_keycloak_startup_recovers_without_credential_logging(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                requests.append("POST")
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(503 if len(requests) == 1 else 200)
                self.end_headers()
                if len(requests) > 1:
                    self.wfile.write(b'{"access_token":"token-canary"}')

            def do_PUT(self):
                requests.append("PUT")
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(204)
                self.end_headers()

            def do_GET(self):
                requests.append("GET")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"internationalizationEnabled": True,
                    "supportedLocales": ["de", "en", "fr", "ru", "ti", "tr"],
                    "defaultLocale": "de", "emailTheme": "oriso"}).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                (Path(directory) / "keycloak_reconcile_smtp.py").write_bytes(HTTP_HELPER.read_bytes())
                (Path(directory) / PYTHON_SCRIPT.name).write_bytes(PYTHON_SCRIPT.read_bytes())
                result = subprocess.run([sys.executable, str(Path(directory) / PYTHON_SCRIPT.name)],
                    env={**os.environ, "KEYCLOAK_URL": f"http://127.0.0.1:{server.server_port}/auth",
                         "KEYCLOAK_REALM": "example", "POD_NAMESPACE": "test",
                         "KEYCLOAK_ADMIN_USERNAME": "admin-canary",
                         "KEYCLOAK_ADMIN_PASSWORD": "secret-canary"},
                    capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(["POST", "POST", "PUT", "GET"], requests)
            self.assertNotIn("secret-canary", result.stdout + result.stderr)
        finally:
            server.shutdown()
            server.server_close()

    def test_failed_admin_authentication_does_not_update_realm(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                requests.append("POST")
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error":"secret-canary"}')

            def do_PUT(self):
                requests.append("PUT")
                self.send_response(204)
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                (Path(directory) / "keycloak_reconcile_smtp.py").write_bytes(HTTP_HELPER.read_bytes())
                (Path(directory) / PYTHON_SCRIPT.name).write_bytes(PYTHON_SCRIPT.read_bytes())
                result = subprocess.run([sys.executable, str(Path(directory) / PYTHON_SCRIPT.name)],
                    env={**os.environ, "KEYCLOAK_URL": f"http://127.0.0.1:{server.server_port}/auth",
                         "KEYCLOAK_REALM": "example", "POD_NAMESPACE": "test",
                         "KEYCLOAK_ADMIN_USERNAME": "admin-canary",
                         "KEYCLOAK_ADMIN_PASSWORD": "secret-canary"},
                    capture_output=True, text=True)
            self.assertEqual(2, result.returncode)
            self.assertEqual("MAIL_LOCALE_AUTH_FAILED", result.stderr.strip())
            self.assertEqual(["POST"], requests)
        finally:
            server.shutdown()
            server.server_close()

    def test_oversized_realm_readback_fails_without_exposing_credentials(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                requests.append("POST")
                self.rfile.read(int(self.headers["Content-Length"]))
                body = b'{"access_token":"token-canary"}'
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def do_PUT(self):
                requests.append("PUT")
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(204)
                self.end_headers()

            def do_GET(self):
                requests.append("GET")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"private":"' + b'x' * 1048576 + b'"}')

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                (Path(directory) / "keycloak_reconcile_smtp.py").write_bytes(HTTP_HELPER.read_bytes())
                (Path(directory) / PYTHON_SCRIPT.name).write_bytes(PYTHON_SCRIPT.read_bytes())
                result = subprocess.run([sys.executable, str(Path(directory) / PYTHON_SCRIPT.name)],
                    env={**os.environ, "KEYCLOAK_URL": f"http://127.0.0.1:{server.server_port}/auth",
                         "KEYCLOAK_REALM": "example", "POD_NAMESPACE": "test",
                         "KEYCLOAK_ADMIN_USERNAME": "admin-canary",
                         "KEYCLOAK_ADMIN_PASSWORD": "secret-canary"},
                    capture_output=True, text=True)
            self.assertEqual(2, result.returncode)
            self.assertEqual("MAIL_LOCALE_REALM_READBACK_FAILED", result.stderr.strip())
            self.assertNotIn("secret-canary", result.stdout + result.stderr)
            self.assertEqual(["POST", "PUT", "GET"], requests)
        finally:
            server.shutdown()
            server.server_close()

    def test_reconciles_existing_realm_without_password_in_process_arguments(self):
        requests = []
        realm = {"internationalizationEnabled": False, "supportedLocales": [],
                 "defaultLocale": "en", "emailTheme": "old",
                 "otherData": "x" * 80000}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def respond(self, status, payload=None):
                body = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                requests.append(("POST", self.path, form, dict(self.headers)))
                if self.path == "/auth/realms/master/protocol/openid-connect/token" and form == {
                    "grant_type": ["password"], "client_id": ["admin-cli"],
                    "username": ["admin-canary"], "password": ["secret-canary"]}:
                    self.respond(200, {"access_token": "token-canary"})
                else:
                    self.respond(401, {"error": "secret-canary"})

            def do_PUT(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(("PUT", self.path, payload, dict(self.headers)))
                if self.headers.get("Authorization") != "Bearer token-canary":
                    return self.respond(403)
                realm.update(payload)
                self.respond(204)

            def do_GET(self):
                requests.append(("GET", self.path, None, dict(self.headers)))
                if self.headers.get("Authorization") != "Bearer token-canary":
                    return self.respond(403)
                # Keycloak stores supportedLocales as a Set, so readback order
                # is not a contract even when all configured locales persisted.
                self.respond(200, {**realm,
                    "supportedLocales": list(reversed(realm["supportedLocales"]))})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                (Path(directory) / "keycloak_reconcile_smtp.py").write_bytes(HTTP_HELPER.read_bytes())
                (Path(directory) / PYTHON_SCRIPT.name).write_bytes(PYTHON_SCRIPT.read_bytes())
                command = [sys.executable, str(Path(directory) / PYTHON_SCRIPT.name)]
                self.assertNotIn("secret-canary", " ".join(command))
                result = subprocess.run(command, env={**os.environ,
                    "KEYCLOAK_URL": f"http://127.0.0.1:{server.server_port}/auth",
                    "KEYCLOAK_REALM": "example", "POD_NAMESPACE": "test",
                    "KEYCLOAK_ADMIN_USERNAME": "admin-canary",
                    "KEYCLOAK_ADMIN_PASSWORD": "secret-canary"}, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotIn("secret-canary", result.stdout + result.stderr)
            self.assertEqual(["POST", "PUT", "GET"], [r[0] for r in requests])
            self.assertEqual({"internationalizationEnabled": True,
                "supportedLocales": ["de", "en", "fr", "ru", "ti", "tr"],
                "defaultLocale": "de", "emailTheme": "oriso"}, requests[1][2])
            self.assertEqual("x" * 80000, realm["otherData"])
        finally:
            server.shutdown()
            server.server_close()
    def test_realm_supports_every_app_mail_language(self):
        realm = json.loads(REALM_PATH.read_text())
        self.assertTrue(realm["internationalizationEnabled"])
        self.assertEqual(
            ["de", "en", "fr", "ru", "ti", "tr"],
            realm["supportedLocales"],
        )
        self.assertEqual("de", realm["defaultLocale"])
        self.assertEqual("oriso", realm["emailTheme"])

    def test_helm_job_reconciles_existing_realms_on_install_and_upgrade(self):
        result = subprocess.run(
            [
                "helm", "template", "mail-locales", str(ROOT),
                "-f", str(ROOT / "values.yaml.default"),
                "-f", str(ROOT / "tests/fixtures/values-render-domain.yaml"),
                "-f", str(ROOT / "secrets.yaml.default"),
                "-f", str(ROOT / "tests/fixtures/render-required-secrets.yaml"),
                "--set-string", "global.secrets.redisdefaultPass=test-redis-password",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        job = next(
            doc for doc in yaml.safe_load_all(result.stdout)
            if doc and doc.get("kind") == "Job"
            and doc["metadata"]["name"] == "keycloak-reconcile-mail-locales"
        )
        self.assertEqual(
            "post-install,post-upgrade", job["metadata"]["annotations"]["helm.sh/hook"]
        )
        self.assertEqual(240, job["spec"]["activeDeadlineSeconds"])
        spec = job["spec"]["template"]["spec"]
        self.assertFalse(spec["automountServiceAccountToken"])
        init = spec["initContainers"][0]
        self.assertEqual("verify-keycloak-mail-theme", init["name"])
        self.assertIn("/opt/keycloak/themes/oriso/email", init["command"][-1])
        self.assertIn("for locale in de en fr ru ti tr", init["command"][-1])
        self.assertTrue(init["securityContext"]["readOnlyRootFilesystem"])
        with tempfile.TemporaryDirectory() as directory:
            theme = Path(directory) / "email"
            (theme / "messages").mkdir(parents=True)
            for locale in ("de", "en", "fr", "ru", "ti", "tr"):
                (theme / "messages" / f"messages_{locale}.properties").write_text("mail=test\n")
            (theme / "theme.properties").write_text("locales=de,en,fr,ru,ti,tr\n")
            gate = lambda: subprocess.run(["sh", "-ec", init["command"][-1]],
                env={**os.environ, "KEYCLOAK_EMAIL_THEME_DIR": str(theme)},
                capture_output=True, text=True)
            self.assertEqual(0, gate().returncode)
            (theme / "messages/messages_ti.properties").unlink()
            self.assertNotEqual(0, gate().returncode)
        container = spec["containers"][0]
        self.assertEqual(["python3", "-B", "/scripts/keycloak-reconcile-mail-locales.py"], container["command"])
        self.assertNotIn("--password", str(container))
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual("keycloak-reconcile-mail-locales-script", spec["volumes"][0]["configMap"]["name"])
        env = {item["name"]: item for item in container["env"]}
        self.assertEqual(
            {"name": "keycloak-secret-env", "key": "KEYCLOAK_ADMIN_PASSWORD"},
            env["KEYCLOAK_ADMIN_PASSWORD"]["valueFrom"]["secretKeyRef"],
        )
        script_map = next(doc for doc in yaml.safe_load_all(result.stdout)
                          if doc and doc.get("kind") == "ConfigMap"
                          and doc["metadata"]["name"] == "keycloak-reconcile-mail-locales-script")
        self.assertEqual(HTTP_HELPER.read_text().strip(),
                         script_map["data"]["keycloak_reconcile_smtp.py"].strip())
        self.assertEqual(PYTHON_SCRIPT.read_text().strip(),
                         script_map["data"]["keycloak-reconcile-mail-locales.py"].strip())


if __name__ == "__main__":
    unittest.main()
