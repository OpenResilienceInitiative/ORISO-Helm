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
    "--set-string", "consultingTypeService.smtpPasswordEncryptionSecret=MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
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
    assert not any(doc.get("kind") == "CronJob" and doc["metadata"]["name"] == "keycloak-reconcile-smtp" for doc in docs)
    deployment = next(doc for doc in docs if doc.get("kind") == "Deployment"
                      and doc["metadata"]["name"] == "keycloak-reconcile-smtp")
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    service = next(doc for doc in docs if doc.get("kind") == "Service"
                   and doc["metadata"]["name"] == "keycloak-reconcile-smtp")
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"][0]["port"] == 8080
    assert job["spec"]["template"]["metadata"]["labels"]["app"] != service["spec"]["selector"]["app"]
    pod = job["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    container = pod["containers"][0]
    assert "@sha256:" in container["image"]
    assert container["command"] == ["python3", "-B", "/scripts/keycloak-reconcile-smtp.py", "--trigger"]
    env = {entry["name"]: entry for entry in container["env"]}
    assert not any(name.startswith("KEYCLOAK_ADMIN_") for name in env)
    assert env["SMTP_RECONCILE_URL"]["value"] == "http://keycloak-reconcile-smtp.default:8080/smtp/reconcile"
    server_pod = deployment["spec"]["template"]["spec"]
    assert server_pod["terminationGracePeriodSeconds"] == 60
    assert server_pod["automountServiceAccountToken"] is False
    server = server_pod["containers"][0]
    assert server["command"] == ["python3", "-B", "/scripts/keycloak-reconcile-smtp.py", "--serve"]
    server_env = {entry["name"]: entry for entry in server["env"]}
    assert "TECHNICAL_PASSWORD" not in server_env
    assert not any(name.startswith("KEYCLOAK_ADMIN_") for name in server_env)
    cts_deployment = next(doc for doc in docs if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "consultingtypeservice")
    cts_env = {entry["name"]: entry for entry in cts_deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    for task_env in (env, server_env, cts_env):
        assert task_env["KEYCLOAK_SMTP_SYNC_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"] == {"name": "oriso-task-identity-credentials", "key": "KEYCLOAK_SMTP_SYNC_CLIENT_SECRET"}
        assert "IDENTITY_TECHNICAL_USER_PASSWORD" not in task_env
        assert not any(name.startswith("KEYCLOAK_ADMIN_") for name in task_env)
    assert not any(name.startswith("SMTP_") and name != "SMTP_RECONCILE_URL" for name in env)
    assert env["IDENTITY_SMTP_SYNC_SERVICE_SUBJECT"]["valueFrom"]["configMapKeyRef"] == {"name": "oriso-task-identity-bindings", "key": "IDENTITY_SMTP_SYNC_SERVICE_SUBJECT"}
    assert env["IDENTITY_SMTP_SYNC_CLIENT_ID"]["valueFrom"]["configMapKeyRef"] == {"name": "oriso-task-identity-bindings", "key": "IDENTITY_SMTP_SYNC_CLIENT_ID"}
    assert env["CONSULTING_TYPE_SERVICE_URL"]["valueFrom"]["configMapKeyRef"] == {"name": "userservice-configmap-env", "key": "CONSULTING_TYPE_SERVICE_API_URL"}
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
