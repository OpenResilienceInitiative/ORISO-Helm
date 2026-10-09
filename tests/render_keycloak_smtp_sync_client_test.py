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
    "--set-string", "global.keycloak.realm=render-realm",
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


LONG_RUNNING = ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "CronJob")


def check_no_long_running_master_admin(docs):
    names = {(doc["kind"], doc["metadata"]["name"]) for doc in docs}
    assert ("Deployment", "keycloak-reconcile-smtp") not in names
    assert ("Service", "keycloak-reconcile-smtp") not in names
    for doc in docs:
        # Only the Keycloak server itself owns its bootstrap admin.
        if doc["kind"] in LONG_RUNNING and doc["metadata"]["name"] != "keycloak":
            text = json.dumps(doc)
            assert "KEYCLOAK_ADMIN" not in text and "keycloak-secret-env" not in text, doc["metadata"]["name"]


def check_sync_pod(pod, realm):
    assert pod["restartPolicy"] == "Never"
    assert pod["automountServiceAccountToken"] is False
    container = pod["containers"][0]
    assert container["command"] == ["python3", "-B", "/scripts/keycloak-reconcile-smtp.py"]
    env = {entry["name"]: entry for entry in container["env"]}
    assert not any(name.startswith("KEYCLOAK_ADMIN") for name in env)
    assert "SMTP_RECONCILE_URL" not in env
    assert env["KEYCLOAK_REALM"]["value"] == realm != "master"
    assert env["SMTP_SYNC_CLIENT_ID"]["value"] == "smtp-sync"
    assert env["SMTP_SYNC_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"] == {
        "name": SECRET_NAME, "key": "KEYCLOAK_SMTP_SYNC_CLIENT_SECRET"}
    # Reading Admin Settings keeps using the technical client; it never writes Keycloak.
    assert env["TECHNICAL_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"]["name"] == "keycloak-backend-client-secrets"


def check_jobs(docs):
    realm = "render-realm"
    hook = find(docs, "Job", "keycloak-reconcile-smtp")
    annotations = hook["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "post-install,post-upgrade"
    # After the service-identity reconcile (weight 15) has created the client.
    assert int(annotations["helm.sh/hook-weight"]) > 15
    assert hook["spec"]["activeDeadlineSeconds"] <= 600
    check_sync_pod(hook["spec"]["template"]["spec"], realm)
    cron = find(docs, "CronJob", "keycloak-reconcile-smtp")
    spec = cron["spec"]
    assert spec["schedule"] == "*/5 * * * *"
    assert spec["concurrencyPolicy"] == "Forbid"
    job = spec["jobTemplate"]["spec"]
    assert job["activeDeadlineSeconds"] <= 240
    assert job["ttlSecondsAfterFinished"] <= 600
    check_sync_pod(job["template"]["spec"], realm)
    result, changed = render("--set-string", "keycloakSmtpReconcile.schedule=*/2 * * * *")
    assert result.returncode == 0, result.stderr
    assert find(changed, "CronJob", "keycloak-reconcile-smtp")["spec"]["schedule"] == "*/2 * * * *"
    # CTS no longer pushes to a helper; a token must never go to a dangling Service name.
    cts = find(docs, "ConfigMap", "consultingtypeservice-configmap-env")
    assert cts["data"]["SMTP_RECONCILE_URL"] == ""


def main():
    result, docs = render()
    assert result.returncode == 0, result.stderr
    check_secret(docs)
    check_no_long_running_master_admin(docs)
    check_jobs(docs)
    print("PASS: SMTP sync uses a short-lived realm client without master admin credentials")


if __name__ == "__main__":
    main()
