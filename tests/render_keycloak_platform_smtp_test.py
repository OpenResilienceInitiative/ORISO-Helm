#!/usr/bin/env python3
"""Keycloak follows the authenticated Admin snapshot and later credential rotation."""

import os
import subprocess

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = [
    "helm", "template", "smtp-test", ROOT,
    "-f", os.path.join(ROOT, "values.yaml.default"),
    "-f", os.path.join(ROOT, "tests", "fixtures", "values-render-domain.yaml"),
    "-f", os.path.join(ROOT, "secrets.yaml.default"),
    "-f", os.path.join(ROOT, "tests", "fixtures", "render-required-secrets.yaml"),
    "--set-string", "global.secrets.redisdefaultPass=test-redis-password",
    "--set-string", "tenantService.smtpPasswordEncryptionSecret=render-test-secret",
    "--set-string", "consultingTypeService.smtpPasswordEncryptionSecret=render-test-secret",
    "--set", "global.domainName=predev.oriso.internal",

]


def render(*extra):
    result = subprocess.run(BASE + list(extra), capture_output=True, text=True)
    return result, [doc for doc in yaml.safe_load_all(result.stdout) if doc] if result.returncode == 0 else []


def main():
    result, docs = render()
    assert result.returncode == 0, result.stderr
    job = next(doc for doc in docs if doc.get("kind") == "Job" and doc["metadata"]["name"] == "keycloak-reconcile-smtp")
    assert job["metadata"]["annotations"]["helm.sh/hook"] == "post-install,post-upgrade"
    cron = next(doc for doc in docs if doc.get("kind") == "CronJob" and doc["metadata"]["name"] == "keycloak-reconcile-smtp")
    assert cron["spec"]["schedule"] == "* * * * *"
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"
    pod = job["spec"]["template"]["spec"]
    assert pod == cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    container = pod["containers"][0]
    assert "@sha256:" in container["image"]
    assert container["command"] == ["python3", "-B", "/scripts/keycloak-reconcile-smtp.py"]
    env = {entry["name"]: entry for entry in container["env"]}
    assert not any(name.startswith("SMTP_") for name in env)
    for name, key in (("TECHNICAL_USERNAME", "IDENTITY_TECHNICAL_USER_USERNAME"),
                      ("TECHNICAL_PASSWORD", "IDENTITY_TECHNICAL_USER_PASSWORD")):
        assert env[name]["valueFrom"]["secretKeyRef"] == {"name": "userservice-secret", "key": key}
    assert env["TECHNICAL_SERVICE_SUBJECT"]["valueFrom"]["configMapKeyRef"] == {
        "name": "tenantservice-configmap-env", "key": "TECHNICAL_SERVICE_SUBJECT"}
    assert env["TECHNICAL_CLIENT_ID"]["valueFrom"]["configMapKeyRef"] == {
        "name": "userservice-configmap-env", "key": "KEYCLOAK_RESOURCE"}
    assert env["CONSULTING_TYPE_SERVICE_URL"]["valueFrom"]["configMapKeyRef"] == {
        "name": "userservice-configmap-env", "key": "CONSULTING_TYPE_SERVICE_API_URL"}
    config = next(doc for doc in docs if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "keycloak-reconcile-smtp-script")
    assert "keycloak-reconcile-smtp.py" in config["data"]
    result, _ = render(
        "--set-string", "userService.smtpHost=legacy.invalid",
        "--set-string", "userService.smtpFrom=legacy@example.invalid",
        "--set-string", "userService.smtpUser=legacy-user-canary",
        "--set-string", "userService.smtpPassword=legacy-password-canary",
    )
    assert result.returncode == 0, result.stderr
    assert "legacy-password-canary" not in result.stdout
    assert "legacy-user-canary" not in result.stdout
    result, _ = render("--set-string", "keycloakSmtpReconcile.image=python:latest")
    assert result.returncode != 0 and "keycloakSmtpReconcile.image" in result.stderr

    print("PASS: Admin-only Keycloak SMTP hook and bounded rotation reconciliation")


if __name__ == "__main__":
    main()
