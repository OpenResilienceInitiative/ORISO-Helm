"""Shared fixture for the real-Keycloak tests (keycloak_*_test.py).

Starts one isolated, digest-pinned Keycloak per test class and always removes it.
Synthetic credentials only. Without Docker the tests skip, unless
ORISO_REQUIRE_KEYCLOAK_DOCKER=1 (CI), where a missing Docker is a failure.
Leftovers of an aborted run: docker rm -f $(docker ps -aq -f label=oriso-helm-test=keycloak)
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml

ROOT = Path(__file__).resolve().parents[1]
# Keycloak 26.6.3, pinned by digest.
IMAGE = "quay.io/keycloak/keycloak@sha256:9b0330756022422149aa6502eb2def8cd47c6e1b000c7c65cdb13e7c0133e992"
INSTALLER = ("synthetic-installer", "synthetic-install-test-credential")
LABEL = "oriso-helm-test=keycloak"
READY_SECONDS = 300
SECRETS = {
    "TECHNICAL_CLIENT_SECRET": "synthetic-technical-client-secret-0001",
    "ADMIN_CLIENT_SECRET": "synthetic-admin-client-secret-000000001",
    "SMTP_SYNC_CLIENT_SECRET": "synthetic-smtp-sync-client-secret-00001",
}
# Fresh realm export UUIDs (runbooks/backend-service-clients.md).
SUBJECTS = {"TECHNICAL_SERVICE_SUBJECT": "12316d09-a9da-41b9-a13e-ee2c515800b5",
            "ADMIN_SERVICE_SUBJECT": "615a7bf8-3e12-40c7-a949-f88640acea8e"}


def require_docker():
    if shutil.which("docker") and subprocess.run(["docker", "info"], capture_output=True).returncode == 0:
        return
    if os.environ.get("ORISO_REQUIRE_KEYCLOAK_DOCKER") == "1":
        raise AssertionError("docker is required for the real-Keycloak tests")
    raise unittest.SkipTest("docker not available")


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


def master_admin(base):
    return request(base, "POST", "/realms/master/protocol/openid-connect/token",
                   {"grant_type": "password", "client_id": "admin-cli",
                    "username": INSTALLER[0], "password": INSTALLER[1]}, form=True)["access_token"]


class KeycloakContainer:
    """One throwaway Keycloak; `base` set to reuse an already running one instead."""

    def __init__(self, prefix, base=None):
        self.name = None
        self.prefix = prefix
        self.base = base

    def start(self):
        if not self.base:
            require_docker()
            self.name = self.prefix + "-" + uuid.uuid4().hex[:10]
            subprocess.run(["docker", "run", "-d", "--name", self.name, "--label", LABEL,
                            "-p", "127.0.0.1::8080",
                            "-e", "JAVA_OPTS_APPEND=-XX:ActiveProcessorCount=2",
                            "-e", "KC_BOOTSTRAP_ADMIN_USERNAME=" + INSTALLER[0],
                            "-e", "KC_BOOTSTRAP_ADMIN_PASSWORD=" + INSTALLER[1],
                            IMAGE, "start-dev", "--http-relative-path=/auth"], check=True, capture_output=True)
        try:
            if self.name:
                endpoint = subprocess.run(["docker", "port", self.name, "8080/tcp"], check=True,
                                          capture_output=True, text=True).stdout.strip().splitlines()[0]
                self.base = "http://" + endpoint + "/auth"
            deadline = time.monotonic() + READY_SECONDS
            while True:
                try:
                    master_admin(self.base)
                    return self
                except (HTTPError, URLError, ConnectionError, TimeoutError, OSError):
                    if time.monotonic() > deadline:
                        raise AssertionError("isolated Keycloak did not become ready") from None
                    time.sleep(2)
        except BaseException:
            self.stop(show_logs=True)
            raise

    def stop(self, show_logs=False):
        if not self.name:
            return
        if show_logs:
            logs = subprocess.run(["docker", "logs", "--tail", "25", self.name], capture_output=True, text=True)
            print(logs.stdout + logs.stderr, file=sys.stderr)
        subprocess.run(["docker", "rm", "-f", self.name], check=True, capture_output=True)
        self.name = None


def rendered_realm():
    """realm.json as the chart renders it, minus ORISO-only providers and themes."""
    command = ["helm", "template", "keycloak-fixture", str(ROOT)]
    for path in ("values.yaml.default", "tests/fixtures/values-render-domain.yaml",
                 "secrets.yaml.default", "tests/fixtures/render-required-secrets.yaml"):
        command.extend(["-f", str(ROOT / path)])
    command.extend(["--set-string", "global.secrets.redisdefaultPass=x",
                    "--set-string", "tenantService.smtpPasswordEncryptionSecret=x"])
    out = subprocess.run(command, check=True, capture_output=True, text=True).stdout
    config = next(doc for doc in yaml.safe_load_all(out)
                  if doc and doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "keycloak-configmap-data")
    realm = json.loads(config["data"]["realm.json"])
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


def run_script(script, env):
    """Run a chart helper from files/; fail on a nonzero exit or a printed secret."""
    result = subprocess.run([sys.executable, "-B", str(ROOT / "files" / script)],
                            env={"PATH": os.environ["PATH"], **env}, capture_output=True, text=True, timeout=120)
    for secret in SECRETS.values():
        assert secret not in result.stdout + result.stderr, "secret printed by " + script
    assert result.returncode == 0, script + ": " + result.stdout + result.stderr
    return result.stdout


def service_identity_env(base, realm, **extra):
    """Environment of the keycloak-reconcile-service-identities hook."""
    return {"KEYCLOAK_URL": base, "KEYCLOAK_REALM": realm, "POD_NAMESPACE": "fixture",
            "KEYCLOAK_ADMIN_USERNAME": INSTALLER[0], "KEYCLOAK_ADMIN_PASSWORD": INSTALLER[1],
            "BOOTSTRAP_ADMIN_USERNAME": INSTALLER[0], "TECHNICAL_CLIENT_ID": "backend-technical",
            "ADMIN_CLIENT_ID": "backend-admin", "HUMAN_CLIENT_ID": "app", "SMTP_SYNC_CLIENT_ID": "smtp-sync",
            "RETIRE_LEGACY_USERS": "false", "LEGACY_TECHNICAL_USERNAME": "technical",
            "LEGACY_ADMIN_USERNAME": "svc-keycloak-admin", **SECRETS, **extra}


def prepared_subjects(base, realm):
    """Existing realm: the runbook's preparation step reports the actual subjects."""
    out = run_script("keycloak-reconcile-service-clients.py",
                     service_identity_env(base, realm, PREPARE_ONLY="true", **SUBJECTS))
    return dict(line.split("=", 1) for line in out.splitlines() if "_SERVICE_SUBJECT=" in line)
