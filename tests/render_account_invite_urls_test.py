#!/usr/bin/env python3
"""Render-test the account-invite link contract for UserService (TEN-INV-U7).

InviteAcceptUrlBuilder (UserService, TEN-INV-U6) derives invite links
server-side: tenant-admin invites get
<admin-base>/admin/tenant-onboarding/{token}, all other roles get
<app-base>/account-invite/{token}. The /admin/tenant-onboarding path is
appended by code, so the admin base URL must be the bare Admin origin —
NOT suffixed with /admin like the password-reset admin base URL.

Where the Admin panel lives on its own host (split-host installs), the
explicit ACCOUNT_INVITE_ADMIN_FRONTEND_BASE_URL must win over the value
derived from global.domainName, or every tenant-admin invite mail links to
the App host where the onboarding route does not exist.
"""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SPLIT_HOST_APP_URL = "https://app.split.oriso.internal"
SPLIT_HOST_ADMIN_URL = "https://admin.split.oriso.internal"
# global.domainName from tests/fixtures/values-render-domain.yaml.
RENDER_DOMAIN = "render.oriso.internal"

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


def render(extra_set_strings: dict[str, str] | None = None) -> list[dict]:
    cmd = [
        "helm",
        "template",
        "account-invite-test",
        CHART_DIR,
        "-f",
        os.path.join(CHART_DIR, "values.yaml.default"),
        "-f",
        os.path.join(CHART_DIR, "tests", "fixtures", "values-render-domain.yaml"),
        "-f",
        os.path.join(CHART_DIR, "secrets.yaml.default"),
    ]
    for key, value in (extra_set_strings or {}).items():
        cmd += ["--set-string", f"{key}={value}"]
    # The default values configure an SMTP transport, whose render gate requires
    # credentials; real deploys carry them in the persistent secret values.
    cmd += [
        "--set-string",
        "userService.smtpUser=smtp-canary-user",
        "--set-string",
        "userService.smtpPassword=smtp-canary-password",
        "--set-string",
        "userService.smtpHost=smtp.canary.example",
        "--set-string",
        "userService.smtpPort=587",
        "--set-string",
        "userService.smtpSecure=false",
        "--set-string",
        "userService.smtpFrom=sender@canary.example",
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


def assert_split_host_invite_urls() -> None:
    docs = render(
        {
            "userService.accountInviteAppFrontendBaseUrl": SPLIT_HOST_APP_URL,
            "userService.accountInviteAdminFrontendBaseUrl": SPLIT_HOST_ADMIN_URL,
        }
    )
    data = userservice_configmap(docs)["data"]

    assert data.get("ACCOUNT_INVITE_APP_FRONTEND_BASE_URL") == SPLIT_HOST_APP_URL, (
        "explicit split-host values must point app invite links at the public "
        f"App host, got {data.get('ACCOUNT_INVITE_APP_FRONTEND_BASE_URL')!r}"
    )
    assert data.get("ACCOUNT_INVITE_ADMIN_FRONTEND_BASE_URL") == SPLIT_HOST_ADMIN_URL, (
        "explicit split-host values must point tenant-admin invite links at the "
        "public Admin origin (code appends /admin/tenant-onboarding), got "
        f"{data.get('ACCOUNT_INVITE_ADMIN_FRONTEND_BASE_URL')!r}"
    )
    assert not data["ACCOUNT_INVITE_ADMIN_FRONTEND_BASE_URL"].endswith("/admin"), (
        "the admin invite base URL must NOT carry the /admin suffix — "
        "InviteAcceptUrlBuilder appends /admin/tenant-onboarding itself"
    )
    print("PASS: explicit split-host values render both account-invite base URLs")

    env_names = userservice_deployment_env_names(docs)
    missing = set(LINK_ENV_KEYS) - env_names
    assert not missing, (
        f"UserService Deployment must import {sorted(missing)} — a ConfigMap "
        "key without an env entry has no effect (ORISO-Helm#128 drift class)"
    )
    print("PASS: UserService Deployment imports both account-invite base URLs")


def assert_reset_links_are_imported() -> None:
    docs = render(
        {
            "userService.magicLinkFrontendBaseUrl": SPLIT_HOST_APP_URL,
            "userService.passwordResetFrontendBaseUrl": SPLIT_HOST_APP_URL,
            "userService.passwordResetAdminFrontendBaseUrl": (
                f"{SPLIT_HOST_ADMIN_URL}/admin"
            ),
        }
    )
    env_names = userservice_deployment_env_names(docs)
    missing = set(RESET_LINK_ENV_KEYS) - env_names
    assert not missing, (
        f"UserService Deployment must import {sorted(missing)}; without the "
        "env import the rendered ConfigMap values never reach the pod and "
        "password reset / Magic Link stay silently disabled"
    )
    print("PASS: UserService Deployment imports magic-link and password-reset URLs")


def assert_upstream_clients_stay_wired() -> None:
    docs = render()
    data = userservice_configmap(docs)["data"]
    env_names = userservice_deployment_env_names(docs)
    for key in UPSTREAM_CLIENT_KEYS:
        assert key in data, f"{key} missing from userservice-configmap-env"
        assert key in env_names, f"UserService Deployment must import {key}"
    print("PASS: TS/AS/CTS client base URLs stay wired ConfigMap -> Deployment")


def assert_derived_when_unset() -> None:
    """Unset invite URLs derive from global.domainName (ORISO-Helm#366).

    They used to be omitted so the service fell back to its own default; the
    service fallbacks are being removed, so the chart must always render the
    keys and import them into the Deployment.
    """
    docs = render()
    data = userservice_configmap(docs)["data"]
    env_names = userservice_deployment_env_names(docs)
    for key in LINK_ENV_KEYS:
        assert data.get(key) == f"https://{RENDER_DOMAIN}", (
            f"{key} must derive from global.domainName when unset, got {data.get(key)!r}"
        )
        assert key in env_names, f"Deployment must import {key}"
    print("PASS: unset invite URLs derive from global.domainName and are imported")


def main() -> None:
    assert_split_host_invite_urls()
    assert_reset_links_are_imported()
    assert_upstream_clients_stay_wired()
    assert_derived_when_unset()


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
