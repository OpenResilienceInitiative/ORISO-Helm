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
SMTP_FROM = "ORISO Platform <monty.burns@oriso.org>"

ENVIRONMENTS = {
    "dev": ("https://dev.oriso.org", "https://dev.oriso.org/admin"),
    "pre-dev": (
        "https://app.oriso-dev.site",
        "https://admin.oriso-dev.site/admin",
    ),
}


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
            "--set-string",
            "userService.smtpUser=smtp-canary-user",
            "--set-string",
            "userService.smtpPassword=smtp-canary-password",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(f"helm template failed:\n{proc.stderr}")
    return [doc for doc in yaml.safe_load_all(proc.stdout) if isinstance(doc, dict)]


def render_environment(
    app_url: str,
    admin_url: str,
    *,
    smtp_user: str | None = "smtp-canary-user",
    smtp_password: str | None = "smtp-canary-password",
) -> subprocess.CompletedProcess:
    """Render explicit environment values; pass ``None`` to omit a credential.

    Real deploys carry both credentials in the persistent secret values; the
    render gate rejects an SMTP transport that lacks either one.
    """
    args = [
        "helm",
        "template",
        "password-reset-env-test",
        CHART_DIR,
        "-f",
        os.path.join(CHART_DIR, "values.yaml.default"),
        "-f",
        os.path.join(CHART_DIR, "secrets.yaml.default"),
        "--set-string",
        f"userService.passwordResetFrontendBaseUrl={app_url}",
        "--set-string",
        f"userService.passwordResetAdminFrontendBaseUrl={admin_url}",
        "--set-string",
        f"userService.smtpFrom={SMTP_FROM}",
    ]
    if smtp_user is not None:
        args += ["--set-string", f"userService.smtpUser={smtp_user}"]
    if smtp_password is not None:
        args += ["--set-string", f"userService.smtpPassword={smtp_password}"]
    return subprocess.run(args, capture_output=True, text=True)


def render_with_environment(app_url: str, admin_url: str) -> list[dict]:
    proc = render_environment(app_url, admin_url)
    if proc.returncode != 0:
        raise AssertionError(
            f"helm template failed for explicit environment values:\n{proc.stderr}"
        )
    return [doc for doc in yaml.safe_load_all(proc.stdout) if isinstance(doc, dict)]


def assert_environment_configures_reset_urls(
    label: str, app_url: str, admin_url: str
) -> None:
    """A deployed environment that leaves these unset sends no reset mail at all."""
    configmaps = [
        doc
        for doc in render_with_environment(app_url, admin_url)
        if doc.get("kind") == "ConfigMap"
    ]
    user_service = next(
        (doc for doc in configmaps if "IDENTITY_OTP_URL" in (doc.get("data") or {})),
        None,
    )
    assert (
        user_service is not None
    ), f"UserService ConfigMap was not rendered for {label}"
    data = user_service["data"]
    assert (
        data.get("PASSWORD_RESET_FRONTEND_BASE_URL") == app_url
    ), f"{label} values must configure the app password-reset base URL"
    assert (
        data.get("PASSWORD_RESET_ADMIN_FRONTEND_BASE_URL") == admin_url
    ), f"{label} values must configure the admin password-reset base URL"
    print(f"PASS: explicit {label} values configure both password-reset base URLs")


def assert_smtp_wiring_renders(
    label: str, app_url: str, admin_url: str, expected_from: str
) -> None:
    """Without SMTP credentials UserService cannot send the reset mail at all."""
    docs = render_with_environment(app_url, admin_url)
    configmaps = [d for d in docs if d.get("kind") == "ConfigMap"]
    user_service = next(
        (d for d in configmaps if "IDENTITY_OTP_URL" in (d.get("data") or {})), None
    )
    assert (
        user_service is not None
    ), f"UserService ConfigMap was not rendered for {label}"
    data = user_service["data"]
    for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_SECURE", "SMTP_FROM"):
        assert (
            key in data
        ), f"{label} values must render {key} into the UserService ConfigMap"
    assert data["SMTP_FROM"] == expected_from

    secret = next(
        (
            d
            for d in docs
            if d.get("kind") == "Secret"
            and d.get("metadata", {}).get("name") == "userservice-secret"
        ),
        None,
    )
    assert secret is not None, "userservice-secret was not rendered"
    for key in ("SMTP_USER", "SMTP_PASSWORD"):
        assert key in (secret.get("data") or {}), f"userservice-secret must carry {key}"

    deployment = next(
        (
            d
            for d in docs
            if d.get("kind") == "Deployment" and "userservice" in d["metadata"]["name"]
        ),
        None,
    )
    assert deployment is not None, "UserService Deployment was not rendered"
    env_entries = {
        entry["name"]: entry
        for entry in deployment["spec"]["template"]["spec"]["containers"][0].get(
            "env", []
        )
    }
    missing = {
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_SECURE",
        "SMTP_FROM",
        "SMTP_USER",
        "SMTP_PASSWORD",
    } - env_entries.keys()
    assert not missing, f"UserService Deployment must import {sorted(missing)}"

    for key in ("SMTP_USER", "SMTP_PASSWORD"):
        entry = env_entries[key]
        ref = (entry.get("valueFrom") or {}).get("secretKeyRef") or {}
        assert "value" not in entry, f"{key} must never render as inline plaintext"
        assert (
            ref.get("name") == "userservice-secret"
        ), f"{key} must read from userservice-secret, got {ref.get('name')!r}"
        assert (
            ref.get("key") == key
        ), f"{key} must read its matching Secret key, got {ref.get('key')!r}"
    print(f"PASS: explicit {label} values wire SMTP transport and credentials")


def assert_smtp_credentials_gate(label: str, app_url: str, admin_url: str) -> None:
    """An SMTP transport lacking either credential must fail the render.

    Deployed with empty credentials, UserService still answers 204 but can
    never authenticate to the relay: password reset silently sends no mail.
    This is exactly what kept reset mails off on dev (ORISO-Helm#179). Each
    credential is omitted independently so a regression from ``or`` to ``and``
    in the template condition cannot slip through.
    """
    cases = {
        "without either SMTP credential": {"smtp_user": None, "smtp_password": None},
        "with only smtpUser missing": {"smtp_user": None},
        "with only smtpPassword missing": {"smtp_password": None},
    }
    for case_label, overrides in cases.items():
        proc = render_environment(app_url, admin_url, **overrides)
        assert (
            proc.returncode != 0
        ), f"{label} values rendered {case_label} — the gate must fail this render"
        assert "smtpUser/smtpPassword" in proc.stderr, (
            f"render failure for {label} {case_label} did not mention the "
            f"missing SMTP credentials:\n{proc.stderr}"
        )
        print(f"PASS: explicit {label} values {case_label} fail the render gate")


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
    assert (
        user_service is not None
    ), "UserService password-reset ConfigMap was not rendered"
    data = user_service["data"]
    assert data["PASSWORD_RESET_FRONTEND_BASE_URL"] == APP_URL
    assert data["PASSWORD_RESET_ADMIN_FRONTEND_BASE_URL"] == ADMIN_URL
    print(
        "PASS: app and admin password-reset URLs render into the UserService ConfigMap"
    )

    without_admin_url = [doc for doc in render("") if doc.get("kind") == "ConfigMap"]
    user_service_without_admin = next(
        doc
        for doc in without_admin_url
        if "PASSWORD_RESET_FRONTEND_BASE_URL" in (doc.get("data") or {})
    )
    assert (
        "PASSWORD_RESET_ADMIN_FRONTEND_BASE_URL"
        not in user_service_without_admin["data"]
    )
    print(
        "PASS: admin password-reset URL is omitted when the environment leaves it unset"
    )

    for label, (app_url, admin_url) in ENVIRONMENTS.items():
        assert_environment_configures_reset_urls(label, app_url, admin_url)
        assert_smtp_wiring_renders(label, app_url, admin_url, SMTP_FROM)
        assert_smtp_credentials_gate(label, app_url, admin_url)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
