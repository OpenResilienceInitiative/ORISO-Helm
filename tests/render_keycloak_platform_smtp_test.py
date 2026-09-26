#!/usr/bin/env python3
"""Keycloak reconciles SMTP from the same deployment values as UserService."""

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
    "--set-string", "userService.smtpHost=smtp.canary.example",
    "--set", "userService.smtpPort=587",
    "--set", "userService.smtpSecure=false",
    "--set-string", "userService.smtpFrom=ORISO Platform <sender@canary.example>",
    "--set-string", "userService.smtpUser=canary-user",
    "--set-string", "userService.smtpPassword=canary-password",
]


def render(*extra):
    result = subprocess.run(BASE + list(extra), capture_output=True, text=True)
    return result, [doc for doc in yaml.safe_load_all(result.stdout) if doc] if result.returncode == 0 else []


def main():
    result, docs = render()
    assert result.returncode == 0, result.stderr
    job = next(doc for doc in docs if doc.get("kind") == "Job" and doc["metadata"]["name"] == "keycloak-reconcile-smtp")
    assert job["metadata"]["annotations"]["helm.sh/hook"] == "post-install,post-upgrade"
    env = {entry["name"]: entry["valueFrom"] for entry in job["spec"]["template"]["spec"]["containers"][0]["env"] if "valueFrom" in entry}
    for name in ("SMTP_HOST", "SMTP_PORT", "SMTP_SECURE", "SMTP_FROM"):
        assert env[name]["configMapKeyRef"] == {"name": "userservice-configmap-env", "key": name}
    for name in ("SMTP_USER", "SMTP_PASSWORD"):
        assert env[name]["secretKeyRef"] == {"name": "userservice-secret", "key": name}
    assert "canary-password" not in result.stdout

    for name in ("smtpHost", "smtpFrom", "smtpUser", "smtpPassword"):
        result, _ = render("--set-string", f"userService.{name}=")
        expected = f"userService.{name}"
        assert result.returncode != 0 and expected in result.stderr, (name, result.stderr)
    print("PASS: Keycloak SMTP hook uses platform config and refuses missing fields")


if __name__ == "__main__":
    main()
