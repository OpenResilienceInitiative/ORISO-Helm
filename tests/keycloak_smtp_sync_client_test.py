#!/usr/bin/env python3
"""Real Keycloak seam for Helm#420; starts and removes its own isolated container.

Proves on a fresh and on an existing realm that the chart's own scripts give the
smtp-sync client exactly manage-realm, and that this client (not master admin)
writes the SMTP settings. Synthetic credentials only. Run separately:
    python3 tests/keycloak_smtp_sync_client_test.py
"""
from contextlib import contextmanager
import base64
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "quay.io/keycloak/keycloak@sha256:9b0330756022422149aa6502eb2def8cd47c6e1b000c7c65cdb13e7c0133e992"
INSTALLER = ("synthetic-installer", "synthetic-install-test-credential")
SECRETS = {
    "TECHNICAL_CLIENT_SECRET": "synthetic-technical-client-secret-0001",
    "ADMIN_CLIENT_SECRET": "synthetic-admin-client-secret-000000001",
    "SMTP_SYNC_CLIENT_SECRET": "synthetic-smtp-sync-client-secret-00001",
}
# Fresh realm export UUIDs (runbooks/backend-service-clients.md).
SUBJECTS = {"TECHNICAL_SERVICE_SUBJECT": "12316d09-a9da-41b9-a13e-ee2c515800b5",
            "ADMIN_SERVICE_SUBJECT": "615a7bf8-3e12-40c7-a949-f88640acea8e"}
SNAPSHOT = {
    "globalFeatureSystemNotificationEmailsEnabled": True, "globalSmtpEnabled": True,
    "globalSmtpHost": "smtp.synthetic.example", "globalSmtpPort": "587", "globalSmtpSecure": False,
    "globalSmtpFrom": "Synthetic Sender <sender@synthetic.example>",
    "globalSmtpUsername": "synthetic-user", "globalSmtpPassword": "synthetic-smtp-password",
}


def request(base, method, path, body=None, token=None, form=False, timeout=10):
    headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = (urlencode(body).encode() if form else json.dumps(body).encode()) if body is not None else None
    with urlopen(Request(base + path, data=data, headers=headers, method=method), timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def status_of(call):
    try:
        call()
    except HTTPError as error:
        error.close()
        return error.code
    return 200


def claims(token):
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def rendered_realm():
    command = ["helm", "template", "smtp-sync", str(ROOT)]
    for path in ("values.yaml.default", "tests/fixtures/values-render-domain.yaml",
                 "secrets.yaml.default", "tests/fixtures/render-required-secrets.yaml"):
        command.extend(["-f", str(ROOT / path)])
    command.extend(["--set-string", "global.secrets.redisdefaultPass=x",
                    "--set-string", "tenantService.smtpPasswordEncryptionSecret=x"])
    out = subprocess.run(command, check=True, capture_output=True, text=True).stdout
    config = next(doc for doc in yaml.safe_load_all(out)
                  if doc and doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "keycloak-configmap-data")
    realm = json.loads(config["data"]["realm.json"])
    # Upstream image has no ORISO authenticators or theme.
    for key in ("authenticationFlows", "authenticatorConfig", "browserFlow", "directGrantFlow",
                "resetCredentialsFlow", "defaultRequiredActions", "requiredActions", "emailTheme",
                "loginTheme", "accountTheme"):
        realm.pop(key, None)
    return realm


def strip_ids(value):
    # A second realm in the same database needs its own generated ids.
    if isinstance(value, dict):
        return {k: strip_ids(v) for k, v in value.items() if k not in ("id", "containerId")}
    if isinstance(value, list):
        return [strip_ids(v) for v in value]
    return value


def without_sync_client(realm):
    realm = strip_ids(realm)
    realm["clients"] = [c for c in realm["clients"] if c["clientId"] != "smtp-sync"]
    realm["users"] = [u for u in realm["users"] if u.get("serviceAccountClientId") != "smtp-sync"]
    realm["clientScopeMappings"]["realm-management"] = [
        m for m in realm["clientScopeMappings"]["realm-management"] if m["client"] != "smtp-sync"]
    return realm


@contextmanager
def fake_admin_settings():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            ok = self.path == "/settingsadmin/smtp-credentials" and self.headers.get("Authorization", "").startswith("Bearer ")
            self.send_response(200 if ok else 403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if ok:
                self.wfile.write(json.dumps(SNAPSHOT).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def isolated_keycloak():
    name = "oriso-smtp-sync-fixture-" + uuid.uuid4().hex[:10]
    subprocess.run(["docker", "run", "-d", "--name", name, "-p", "127.0.0.1::8080",
                    "-e", "JAVA_OPTS_APPEND=-XX:ActiveProcessorCount=2",
                    "-e", "KC_BOOTSTRAP_ADMIN_USERNAME=" + INSTALLER[0],
                    "-e", "KC_BOOTSTRAP_ADMIN_PASSWORD=" + INSTALLER[1],
                    IMAGE, "start-dev", "--http-relative-path=/auth"], check=True, capture_output=True)
    try:
        endpoint = subprocess.run(["docker", "port", name, "8080/tcp"], check=True,
                                  capture_output=True, text=True).stdout.strip().splitlines()[0]
        base = "http://" + endpoint + "/auth"
        deadline = time.monotonic() + 300
        while True:
            try:
                master_admin(base)
                break
            except (HTTPError, URLError, ConnectionError, TimeoutError):
                if time.monotonic() > deadline:
                    raise AssertionError("isolated Keycloak did not become ready") from None
                time.sleep(2)
        yield base
    except Exception:
        logs = subprocess.run(["docker", "logs", "--tail", "25", name], capture_output=True, text=True)
        print(logs.stdout + logs.stderr)
        raise
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True)


def master_admin(base):
    return request(base, "POST", "/realms/master/protocol/openid-connect/token",
                   {"grant_type": "password", "client_id": "admin-cli",
                    "username": INSTALLER[0], "password": INSTALLER[1]}, form=True)["access_token"]


def run_script(script, env):
    result = subprocess.run([sys.executable, "-B", str(ROOT / "files" / script)],
                            env={"PATH": os.environ["PATH"], **env}, capture_output=True, text=True, timeout=120)
    for secret in SECRETS.values():
        assert secret not in result.stdout + result.stderr, "secret printed by " + script
    assert result.returncode == 0, script + ": " + result.stdout + result.stderr
    return result.stdout


def prove(base, realm_name, cts, subjects):
    common = {"KEYCLOAK_URL": base, "KEYCLOAK_REALM": realm_name, "POD_NAMESPACE": "fixture",
              "TECHNICAL_CLIENT_ID": "backend-technical", "SMTP_SYNC_CLIENT_ID": "smtp-sync", **SECRETS}
    hook = {**common, "KEYCLOAK_ADMIN_USERNAME": INSTALLER[0], "KEYCLOAK_ADMIN_PASSWORD": INSTALLER[1],
            "BOOTSTRAP_ADMIN_USERNAME": INSTALLER[0], "ADMIN_CLIENT_ID": "backend-admin", "HUMAN_CLIENT_ID": "app",
            "RETIRE_LEGACY_USERS": "false", "LEGACY_TECHNICAL_USERNAME": "technical",
            "LEGACY_ADMIN_USERNAME": "svc-keycloak-admin"}
    if subjects is None:
        # Existing realm: the runbook's preparation step reports the actual subjects.
        out = run_script("keycloak-reconcile-service-clients.py", {**hook, **SUBJECTS, "PREPARE_ONLY": "true"})
        subjects = dict(line.split("=", 1) for line in out.splitlines() if line.endswith(tuple("0123456789abcdef"))
                        and "_SERVICE_SUBJECT=" in line)
    common.update(subjects)
    # Service-identity hook (the only step that still uses the master installer).
    out = run_script("keycloak-reconcile-service-clients.py", {**hook, **subjects, "PREPARE_ONLY": "false"})
    assert "SMTP_SYNC_CLIENT_RECONCILED" in out, out
    # SMTP Job: no master credential in its environment at all.
    out = run_script("keycloak-reconcile-smtp.py", {**common, "CONSULTING_TYPE_SERVICE_URL": cts})
    assert "SMTP_RECONCILE_APPLIED" in out, out
    smtp = request(base, "GET", "/admin/realms/" + realm_name, token=master_admin(base))["smtpServer"]
    assert smtp["host"] == "smtp.synthetic.example" and smtp["from"] == "sender@synthetic.example", smtp
    assert smtp["fromDisplayName"] == "Synthetic Sender" and smtp["user"] == "synthetic-user", smtp
    assert smtp["starttls"] == "true" and smtp["port"] == "587", smtp
    # Narrowness: realm settings only, no users, no master realm.
    sync = request(base, "POST", "/realms/" + realm_name + "/protocol/openid-connect/token",
                   {"grant_type": "client_credentials", "client_id": "smtp-sync",
                    "client_secret": SECRETS["SMTP_SYNC_CLIENT_SECRET"]}, form=True)["access_token"]
    issued = claims(sync)
    assert issued["resource_access"] == {"realm-management": {"roles": ["manage-realm"]}}, issued
    assert not issued.get("realm_access", {}).get("roles"), issued
    assert status_of(lambda: request(base, "GET", "/admin/realms/" + realm_name + "/users", token=sync)) == 403
    assert status_of(lambda: request(base, "GET", "/admin/realms/master", token=sync)) in (401, 403)
    master_login = status_of(lambda: request(base, "POST", "/realms/master/protocol/openid-connect/token",
                                             {"grant_type": "client_credentials", "client_id": "smtp-sync",
                                              "client_secret": SECRETS["SMTP_SYNC_CLIENT_SECRET"]}, form=True))
    assert master_login in (400, 401), master_login


def main():
    realm = rendered_realm()
    with isolated_keycloak() as base, fake_admin_settings() as cts:
        admin = master_admin(base)
        fresh = dict(realm, realm="smtp-fresh")
        fresh.pop("id", None)
        request(base, "POST", "/admin/realms", fresh, admin, timeout=90)
        prove(base, "smtp-fresh", cts, SUBJECTS)
        existing = dict(without_sync_client(realm), realm="smtp-existing")
        existing.pop("id", None)
        request(base, "POST", "/admin/realms", existing, master_admin(base), timeout=90)
        prove(base, "smtp-existing", cts, None)
        master_clients = request(base, "GET", "/admin/realms/master/clients?clientId=smtp-sync", token=master_admin(base))
        assert master_clients == [], "smtp-sync must not exist in master"
    print("PASS: smtp-sync realm client writes SMTP on fresh and existing realms; no master access")


if __name__ == "__main__":
    main()
