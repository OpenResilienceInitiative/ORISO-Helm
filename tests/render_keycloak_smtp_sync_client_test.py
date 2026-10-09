#!/usr/bin/env python3
"""SMTP sync runs as short Jobs with a realm client, never with the master admin (Helm#420)."""

import base64
import json
import os
import subprocess

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = [
    "helm", "template", "smtp-sync", ROOT,
    "-f", os.path.join(ROOT, "values.yaml.default"),
    "-f", os.path.join(ROOT, "tests", "fixtures", "values-render-domain.yaml"),
    "-f", os.path.join(ROOT, "secrets.yaml.default"),
    "-f", os.path.join(ROOT, "tests", "fixtures", "render-required-secrets.yaml"),
    "--set-string", "global.secrets.redisdefaultPass=test-redis-password",
    "--set-string", "tenantService.smtpPasswordEncryptionSecret=render-test-secret",
    "--set", "global.domainName=predev.oriso.internal",
]
SECRET_NAME = "keycloak-smtp-sync-client"
FIXTURE_SECRET = "render-only-smtp-sync-client-secret-canary"


def render(*extra):
    result = subprocess.run(BASE + list(extra), capture_output=True, text=True)
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc] if result.returncode == 0 else []
    return result, docs


def find(docs, kind, name):
    return next(doc for doc in docs if doc.get("kind") == kind and doc["metadata"]["name"] == name)


def check_secret(docs):
    secret = find(docs, "Secret", SECRET_NAME)
    assert base64.b64decode(secret["data"]["KEYCLOAK_SMTP_SYNC_CLIENT_SECRET"]).decode() == FIXTURE_SECRET
    # Backend services never receive the realm-settings credential.
    backend = find(docs, "Secret", "keycloak-backend-client-secrets")
    assert not any("SMTP" in key for key in backend["data"])
    for value in ("", "short-secret", "render-only-technical-client-secret-canary",
                  "render-only-admin-client-secret-canary"):
        result, _ = render("--set-string", "global.secrets.keycloakSmtpSyncClientSecret=" + value)
        assert result.returncode != 0, "unsafe SMTP sync secret accepted: " + repr(value)
        assert "keycloakSmtpSyncClientSecret" in result.stderr, result.stderr


def main():
    result, docs = render()
    assert result.returncode == 0, result.stderr
    check_secret(docs)
    print("PASS: SMTP sync uses a short-lived realm client without master admin credentials")


if __name__ == "__main__":
    main()
