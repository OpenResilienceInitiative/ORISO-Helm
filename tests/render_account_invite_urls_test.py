#!/usr/bin/env python3
"""Render-test the account-invite link contract for UserService (TEN-INV-U7).

InviteAcceptUrlBuilder (UserService, TEN-INV-U6) derives invite links
server-side: tenant-admin invites get
<admin-base>/admin/tenant-onboarding/{token}, all other roles get
<app-base>/account-invite/{token}. The /admin/tenant-onboarding path is
appended by code, so the admin base URL must be the bare Admin origin —
NOT suffixed with /admin like the password-reset admin base URL.

On Pre-Dev the Admin panel lives on its own host (admin.oriso-dev.site,
ingress ground truth: ORISO-Kubernetes/ingress/14-admin-ingress.yaml), so
ACCOUNT_INVITE_ADMIN_FRONTEND_BASE_URL must be set there or every
tenant-admin invite mail links to the App host where the onboarding route
does not exist.
"""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PRE_DEV_APP_URL = "https://app.oriso-dev.site"
PRE_DEV_ADMIN_URL = "https://admin.oriso-dev.site"

LINK_ENV_KEYS = (
    "ACCOUNT_INVITE_APP_FRONTEND_BASE_URL",
    "ACCOUNT_INVITE_ADMIN_FRONTEND_BASE_URL",
)

# The invite creation path (TEN-INV-U3) reserves IDs through these clients;
# the mail path (TEN-INV-U6) reads global SMTP settings from CTS. All three
# base URLs must stay wired ConfigMap -> Deployment env.
UPSTREAM_CLIENT_KEYS = (
    "TENANT_SERVICE_API_URL",
    "AGENCY_ADMIN_SERVICE_API_URL",
    "CONSULTING_TYPE_SERVICE_API_URL",
)

# Fail-closed link keys that already render into the ConfigMap: without a
# Deployment env import they silently have no effect (drift class of
# ORISO-Helm#128).
RESET_LINK_ENV_KEYS = (
    "MAGIC_LINK_FRONTEND_BASE_URL",
    "PASSWORD_RESET_FRONTEND_BASE_URL",
    "PASSWORD_RESET_ADMIN_FRONTEND_BASE_URL",
)


def render(extra_values_files: list[str] | None = None) -> list[dict]:
    cmd = [
        "helm",
        "template",
        "account-invite-test",
        CHART_DIR,
        "-f",
        os.path.join(CHART_DIR, "values.yaml.default"),
        "-f",
        os.path.join(CHART_DIR, "secrets.yaml.default"),
    ]
    for values_file in extra_values_files or []:
        cmd += ["-f", os.path.join(CHART_DIR, values_file)]
    if extra_values_files:
        # The environment overlays configure an SMTP transport, whose render
        # gate requires credentials; real deploys carry them in the persistent
        # secret values. Default renders stay credential-free like the real
        # default configuration.
        cmd += [
            "--set-string",
            "userService.smtpUser=smtp-canary-user",
            "--set-string",
            "userService.smtpPassword=smtp-canary-password",
        ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"helm template failed:\n{proc.stderr}")
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


def userservice_deployment_env_names(docs: list[dict]) -> set[str]:
    deployment = next(
        (
            doc
            for doc in docs
            if doc.get("kind") == "Deployment"
            and "userservice" in doc["metadata"]["name"]
        ),
        None,
    )
    assert deployment is not None, "UserService Deployment was not rendered"
    return {
        entry["name"]
        for entry in deployment["spec"]["template"]["spec"]["containers"][0].get(
            "env", []
        )
    }


def assert_pre_dev_invite_urls() -> None:
    docs = render(["values-pre-dev.yaml"])
    data = userservice_configmap(docs)["data"]

    assert data.get("ACCOUNT_INVITE_APP_FRONTEND_BASE_URL") == PRE_DEV_APP_URL, (
        "values-pre-dev.yaml must point the app invite links at the public "
        f"App host, got {data.get('ACCOUNT_INVITE_APP_FRONTEND_BASE_URL')!r}"
    )
    assert data.get("ACCOUNT_INVITE_ADMIN_FRONTEND_BASE_URL") == PRE_DEV_ADMIN_URL, (
        "values-pre-dev.yaml must point tenant-admin invite links at the "
        "public Admin origin (code appends /admin/tenant-onboarding), got "
        f"{data.get('ACCOUNT_INVITE_ADMIN_FRONTEND_BASE_URL')!r}"
    )
    assert not data["ACCOUNT_INVITE_ADMIN_FRONTEND_BASE_URL"].endswith("/admin"), (
        "the admin invite base URL must NOT carry the /admin suffix — "
        "InviteAcceptUrlBuilder appends /admin/tenant-onboarding itself"
    )
    print("PASS: values-pre-dev.yaml renders both account-invite base URLs")

    env_names = userservice_deployment_env_names(docs)
    missing = set(LINK_ENV_KEYS) - env_names
    assert not missing, (
        f"UserService Deployment must import {sorted(missing)} — a ConfigMap "
        "key without an env entry has no effect (ORISO-Helm#128 drift class)"
    )
    print("PASS: UserService Deployment imports both account-invite base URLs")


def assert_reset_links_are_imported() -> None:
    docs = render(["values-pre-dev.yaml"])
    env_names = userservice_deployment_env_names(docs)
    missing = set(RESET_LINK_ENV_KEYS) - env_names
    assert not missing, (
        f"UserService Deployment must import {sorted(missing)}; without the "
        "env import the rendered ConfigMap values never reach the pod and "
        "password reset / Magic Link stay silently disabled"
    )
    print("PASS: UserService Deployment imports magic-link and password-reset URLs")


def assert_upstream_clients_stay_wired() -> None:
    docs = render(["values-pre-dev.yaml"])
    data = userservice_configmap(docs)["data"]
    env_names = userservice_deployment_env_names(docs)
    for key in UPSTREAM_CLIENT_KEYS:
        assert key in data, f"{key} missing from userservice-configmap-env"
        assert key in env_names, f"UserService Deployment must import {key}"
    print("PASS: TS/AS/CTS client base URLs stay wired ConfigMap -> Deployment")


def assert_omitted_when_unset() -> None:
    """No half-wiring: unset invite URLs must not render keys or env imports.

    A configMapKeyRef pointing at a key the ConfigMap does not carry makes the
    pod fail to start, so the env import must be guarded exactly like the key.
    """
    docs = render()
    data = userservice_configmap(docs)["data"]
    env_names = userservice_deployment_env_names(docs)
    for key in LINK_ENV_KEYS:
        assert key not in data, (
            f"{key} must be omitted when the environment leaves it unset so "
            "the app-side fallback (system notification base URL) applies"
        )
        assert key not in env_names, (
            f"Deployment must not reference {key} when the ConfigMap omits it"
        )
    print("PASS: invite URL keys and env imports are omitted when unset")


def main() -> None:
    assert_pre_dev_invite_urls()
    assert_reset_links_are_imported()
    assert_upstream_clients_stay_wired()
    assert_omitted_when_unset()


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
