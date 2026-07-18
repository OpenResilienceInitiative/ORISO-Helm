#!/usr/bin/env python3
"""Render-test the app/admin password-reset URL contract for UserService."""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_URL = "https://app.reset-canary.example"
ADMIN_URL = "https://admin.reset-canary.example/admin"


def render(admin_url: str = ADMIN_URL) -> list[dict]:
    admin_value = f"userService.passwordResetAdminFrontendBaseUrl={admin_url}"
    proc = subprocess.run(
        [
            "helm",
            "template",
            "password-reset-test",
            CHART_DIR,
            "-f",
            os.path.join(CHART_DIR, "values.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "secrets.yaml.default"),
            "--set-string",
            f"userService.passwordResetFrontendBaseUrl={APP_URL}",
            "--set-string",
            admin_value,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(f"helm template failed:\n{proc.stderr}")
    return [doc for doc in yaml.safe_load_all(proc.stdout) if isinstance(doc, dict)]


def main() -> None:
    configmaps = [doc for doc in render() if doc.get("kind") == "ConfigMap"]
    user_service = next(
        (
            doc
            for doc in configmaps
            if "PASSWORD_RESET_FRONTEND_BASE_URL" in (doc.get("data") or {})
        ),
        None,
    )
    assert user_service is not None, "UserService password-reset ConfigMap was not rendered"
    data = user_service["data"]
    assert data["PASSWORD_RESET_FRONTEND_BASE_URL"] == APP_URL
    assert data["PASSWORD_RESET_ADMIN_FRONTEND_BASE_URL"] == ADMIN_URL
    print("PASS: app and admin password-reset URLs render into the UserService ConfigMap")

    without_admin_url = [doc for doc in render("") if doc.get("kind") == "ConfigMap"]
    user_service_without_admin = next(
        doc
        for doc in without_admin_url
        if "PASSWORD_RESET_FRONTEND_BASE_URL" in (doc.get("data") or {})
    )
    assert "PASSWORD_RESET_ADMIN_FRONTEND_BASE_URL" not in user_service_without_admin["data"]
    print("PASS: admin password-reset URL is omitted when the environment leaves it unset")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
