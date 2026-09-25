#!/usr/bin/env python3
"""TenantService's constrained mail endpoint receives only explicit identity pins."""

import os
import subprocess

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = [
    "helm", "template", "tenant-system-email", ROOT,
    "-f", os.path.join(ROOT, "values.yaml.default"),
    "-f", os.path.join(ROOT, "tests", "fixtures", "values-render-domain.yaml"),
    "-f", os.path.join(ROOT, "secrets.yaml.default"),
    "--set-string", "global.secrets.redisdefaultPass=test-redis-password",
    "--set-string", "tenantService.smtpPasswordEncryptionSecret=render-test-secret",
    "--set-string", "consultingTypeService.smtpPasswordEncryptionSecret=render-test-secret",
    "--set-string", "userService.smtpUser=smtp-canary-user",
    "--set-string", "userService.smtpPassword=smtp-canary-password",
]


def render(*overrides):
    result = subprocess.run(BASE + list(overrides), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def resource(documents, kind, name):
    return next(doc for doc in documents if doc.get("kind") == kind and doc["metadata"]["name"] == name)


def test_blank_pins_stay_blank():
    documents = render()
    data = resource(documents, "ConfigMap", "tenantservice-configmap-env")["data"]
    assert data["SYSTEM_EMAIL_DELIVERY_SERVICE_SUBJECT"] == ""
    assert data["SYSTEM_EMAIL_DELIVERY_SERVICE_CLIENT"] == ""


def test_exact_pins_reach_tenantservice_only():
    documents = render(
        "--set-string", "tenantService.systemEmailDeliveryServiceSubject=realm-user-uuid",
        "--set-string", "tenantService.systemEmailDeliveryServiceClient=app",
    )
    data = resource(documents, "ConfigMap", "tenantservice-configmap-env")["data"]
    assert data["SYSTEM_EMAIL_DELIVERY_SERVICE_SUBJECT"] == "realm-user-uuid"
    assert data["SYSTEM_EMAIL_DELIVERY_SERVICE_CLIENT"] == "app"
    deployment = resource(documents, "Deployment", "tenantservice")
    env = {entry["name"]: entry for entry in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    for name in ("SYSTEM_EMAIL_DELIVERY_SERVICE_SUBJECT", "SYSTEM_EMAIL_DELIVERY_SERVICE_CLIENT"):
        assert env[name]["valueFrom"]["configMapKeyRef"] == {
            "name": "tenantservice-configmap-env", "key": name
        }
    assert "realm-user-uuid" not in str(resource(documents, "ConfigMap", "userservice-configmap-env"))


if __name__ == "__main__":
    test_blank_pins_stay_blank()
    test_exact_pins_reach_tenantservice_only()
    print("PASS: tenant mail identity pins are explicit and reach TenantService")
