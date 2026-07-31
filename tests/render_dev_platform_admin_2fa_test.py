#!/usr/bin/env python3
"""Render-test the Dev platform-admin App-TOTP policy."""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OTP_KEY = "IDENTITY_OTP_ALLOWED_FOR_TENANT_SUPER_ADMINS"


def render(values_file: str) -> list[dict]:
    proc = subprocess.run(
        [
            "helm",
            "template",
            "platform-admin-2fa-test",
            CHART_DIR,
            "-f",
            os.path.join(CHART_DIR, "values.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "secrets.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, values_file),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(f"helm template failed for {values_file}:\n{proc.stderr}")
    return [doc for doc in yaml.safe_load_all(proc.stdout) if isinstance(doc, dict)]


def userservice_configmap(docs: list[dict]) -> dict:
    configmap = next(
        (
            doc
            for doc in docs
            if doc.get("kind") == "ConfigMap"
            and doc.get("metadata", {}).get("name") == "userservice-configmap-env"
        ),
        None,
    )
    assert configmap is not None, "userservice-configmap-env was not rendered"
    return configmap


def userservice_deployment(docs: list[dict]) -> dict:
    deployment = next(
        (
            doc
            for doc in docs
            if doc.get("kind") == "Deployment"
            and "userservice" in doc.get("metadata", {}).get("name", "")
        ),
        None,
    )
    assert deployment is not None, "UserService Deployment was not rendered"
    return deployment


def assert_dev_enables_platform_admin_2fa() -> None:
    docs = render("values-dev.yaml")
    data = userservice_configmap(docs).get("data", {})
    assert data.get(OTP_KEY) == "true", (
        "values-dev.yaml must enable real App-TOTP for platform administrators"
    )

    deployment = userservice_deployment(docs)
    env = deployment["spec"]["template"]["spec"]["containers"][0].get("env", [])
    entry = next((item for item in env if item.get("name") == OTP_KEY), None)
    assert entry is not None, f"UserService Deployment must import {OTP_KEY}"
    key_ref = entry.get("valueFrom", {}).get("configMapKeyRef", {})
    assert key_ref == {"name": "userservice-configmap-env", "key": OTP_KEY}, (
        f"{OTP_KEY} must come from the userservice-configmap-env key of the same name"
    )
    print("PASS: Dev enables and wires platform-admin App-TOTP")


def assert_production_is_unchanged() -> None:
    docs = render("values-prod.yaml")
    data = userservice_configmap(docs).get("data", {})
    assert data.get(OTP_KEY) == "false", (
        "this Dev repair must not change the production platform-admin OTP policy"
    )
    print("PASS: production platform-admin OTP policy remains unchanged")


def main() -> None:
    assert_dev_enables_platform_admin_2fa()
    assert_production_is_unchanged()


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
