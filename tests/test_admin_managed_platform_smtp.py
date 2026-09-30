"""Fresh installs keep platform SMTP in Admin Settings, not chart values."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml


CHART = Path(__file__).resolve().parents[1]
SMTP_KEYS = {
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_SECURE",
    "SMTP_FROM",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_REQUIRED",
}


@pytest.mark.parametrize("legacy_values", [False, True])
def test_platform_smtp_is_configured_after_install_in_admin_settings(
    legacy_values: bool,
) -> None:
    args = [
        "helm",
        "template",
        "admin-smtp-install-test",
        str(CHART),
        "-f",
        str(CHART / "values.yaml.default"),
        "-f",
        str(CHART / "tests/fixtures/values-render-domain.yaml"),
        "-f",
        str(CHART / "secrets.yaml.default"),
        "-f",
        str(CHART / "tests/fixtures/render-required-secrets.yaml"),
    ]
    if legacy_values:
        args += [
            "--set-string",
            "userService.smtpHost=smtp.legacy.internal",
            "--set-string",
            "userService.smtpFrom=sender@legacy.internal",
            "--set-string",
            "userService.smtpUser=legacy-user",
            "--set-string",
            "userService.smtpPassword=legacy-test-password",
        ]
    else:
        args += [
            "--set-string",
            "userService.smtpHost=",
            "--set-string",
            "userService.smtpFrom=",
            "--set-string",
            "userService.smtpPort=",
            "--set-string",
            "userService.smtpSecure=",
        ]

    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]
    config = next(
        doc for doc in docs if doc.get("metadata", {}).get("name") == "userservice-configmap-env"
    )
    secret = next(
        doc for doc in docs if doc.get("metadata", {}).get("name") == "userservice-secret"
    )
    deployment = next(
        doc for doc in docs if doc.get("metadata", {}).get("name") == "userservice"
        and doc.get("kind") == "Deployment"
    )
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]

    assert SMTP_KEYS.isdisjoint(config["data"])
    assert SMTP_KEYS.isdisjoint(secret["data"])
    assert SMTP_KEYS.isdisjoint(entry["name"] for entry in env)
    assert config["data"]["CONSULTING_TYPE_SERVICE_API_URL"]
    assert secret["data"]["IDENTITY_TECHNICAL_USER_USERNAME"]
    assert secret["data"]["IDENTITY_TECHNICAL_USER_PASSWORD"]
