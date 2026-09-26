#!/usr/bin/env python3
"""Render guard: public URLs come from global.domainName or fail the install.

Rule (ORISO-Helm#366/#368): a deployed service never invents a URL. Helm is
the first gate, so `helm template` must stop when `global.domainName` is
missing or still a placeholder, and every public URL the chart hands to a
service (mail links, app base URLs, Keycloak hostname and app origin, Synapse
public_baseurl, frontend config) must resolve to the configured domain.

The committed environment overlays must keep rendering on their own: this
chart change merges before the fail-fast service changes that start to
require the new env vars.
"""

from __future__ import annotations

import os
import subprocess
import sys
from urllib.parse import urlparse

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SMTP_CREDENTIALS = (
    "--set-string",
    "userService.smtpHost=smtp.render.oriso.internal",
    "--set-string",
    "userService.smtpFrom=ORISO Render <sender@render.oriso.internal>",
    "--set-string",
    "userService.smtpUser=smtp-canary-user",
    "--set-string",
    "userService.smtpPassword=smtp-canary-password",
)

# A valid Matrix identity; values.yaml.default only ships placeholders, which
# fail the install. Later --set-string arguments override these.
MATRIX_IDENTITY = (
    "--set-string",
    "matrix.matrixServerName=matrix.render.oriso.internal",
    "--set-string",
    "matrix.synapseServerName=matrix.render.oriso.internal",
    "--set-string",
    "global.matrix.matrixServerName=matrix.render.oriso.internal",
    "--set-string",
    "matrixrtcAuth.membershipReaderUserId=@matrixrtc-auth:matrix.render.oriso.internal",
)

# Every mail-link / public origin env the UserService reads (application.properties
# on ORISO-UserService dev). All of them must be rendered unconditionally, so the
# service can drop its localhost / app.base.url fallbacks.
USERSERVICE_PUBLIC_URL_KEYS = (
    "APP_BASE_URL",
    "SYSTEM_NOTIFICATION_FRONTEND_BASE_URL",
    "DPA_SIGN_FRONTEND_BASE_URL",
    "MAGIC_LINK_FRONTEND_BASE_URL",
    "PASSWORD_RESET_FRONTEND_BASE_URL",
    "PASSWORD_RESET_ADMIN_FRONTEND_BASE_URL",
    "ACCOUNT_INVITE_APP_FRONTEND_BASE_URL",
    "ACCOUNT_INVITE_ADMIN_FRONTEND_BASE_URL",
    "INACTIVE_ACCOUNT_NOTIFICATION_APP_BASE_URL",
)

# ConfigMap name -> public URL keys that must point at the configured domain.
PUBLIC_URL_KEYS_BY_CONFIGMAP = {
    "userservice-configmap-env": USERSERVICE_PUBLIC_URL_KEYS,
    "consultingtypeservice-configmap-env": ("APP_BASE_URL", "DPA_SIGN_FRONTEND_BASE_URL"),
    "tenantservice-configmap-env": ("APP_BASE_URL",),
    "keycloak-configmap-env": ("KEYCLOAK_HOSTNAME", "ORISO_APP_BASE_URL"),
    "frontend-configmap": (
        "REACT_APP_API_URL",
        "VITE_API_URL",
        "REACT_APP_MATRIX_HOMESERVER_URL",
        "VITE_MATRIX_HOMESERVER_URL",
        "REACT_APP_ELEMENT_CALL_URL",
        "REACT_APP_ELEMENT_CALL_BASE_URL",
    ),
    "admin-configmap": ("VITE_API_URL",),
}

PLACEHOLDER_MARKERS = ("your-domain", "example.com", "localhost", "matrix-synapse")

# Committed overlays: expected domain, and the UserService keys the overlay
# sets explicitly to another host (Pre-Dev serves Admin on its own host).
OVERLAYS = {
    "values-dev.yaml": {
        "domain": "dev.oriso.org",
        "explicit": {
            "PASSWORD_RESET_ADMIN_FRONTEND_BASE_URL": "https://dev.oriso.org/admin",
        },
    },
    "values-pre-dev.yaml": {
        "domain": "predev.oriso.org",
        "explicit": {},
    },
}

# The production overlay names no host at all: the operator supplies
# global.domainName at install time, and the install fails without it.
PROD_OVERLAY = "values-prod.yaml"

# Render-test host. Reserved example domains (example.com/.org/.net/.test) and
# .invalid are rejected as placeholders, so tests use the private .internal TLD.
VALID_TEST_DOMAIN = "render.oriso.internal"


def helm_template(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "helm",
            "template",
            "required-public-urls",
            CHART_DIR,
            "-f",
            os.path.join(CHART_DIR, "values.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "secrets.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "tests", "fixtures", "render-required-secrets.yaml"),
            *SMTP_CREDENTIALS,
            *MATRIX_IDENTITY,
            *args,
        ],
        capture_output=True,
        text=True,
    )


def rendered(proc: subprocess.CompletedProcess, label: str) -> list[dict]:
    if proc.returncode != 0:
        raise AssertionError(f"helm template failed for {label}:\n{proc.stderr}")
    return [doc for doc in yaml.safe_load_all(proc.stdout) if isinstance(doc, dict)]


def configmap_data(docs: list[dict], name: str) -> dict:
    for doc in docs:
        if doc.get("kind") == "ConfigMap" and doc.get("metadata", {}).get("name") == name:
            return doc.get("data") or {}
    raise AssertionError(f"ConfigMap {name} was not rendered")


def deployment_env_names(docs: list[dict], name_fragment: str) -> set[str]:
    for doc in docs:
        if doc.get("kind") == "Deployment" and name_fragment in doc["metadata"]["name"]:
            containers = doc["spec"]["template"]["spec"]["containers"]
            return {entry["name"] for entry in containers[0].get("env", [])}
    raise AssertionError(f"Deployment matching {name_fragment!r} was not rendered")


def synapse_public_baseurl(docs: list[dict]) -> str:
    for doc in docs:
        if doc.get("kind") != "ConfigMap":
            continue
        for content in (doc.get("data") or {}).values():
            if not isinstance(content, str) or "public_baseurl:" not in content:
                continue
            for line in content.splitlines():
                if line.strip().startswith("public_baseurl:"):
                    return line.split(":", 1)[1].strip().strip("\"'")
    raise AssertionError("Synapse homeserver config with public_baseurl was not rendered")


def assert_fails_naming(proc: subprocess.CompletedProcess, value_name: str, label: str) -> None:
    assert proc.returncode != 0, f"{label}: helm template must fail, but it rendered"
    assert value_name in proc.stderr, (
        f"{label}: the failure must name {value_name}, got:\n{proc.stderr[-800:]}"
    )


def test_default_values_fail_naming_domain() -> None:
    assert_fails_naming(helm_template(), "global.domainName", "values.yaml.default unchanged")
    print("PASS: values.yaml.default alone fails and names global.domainName")


def test_placeholder_or_malformed_domains_fail() -> None:
    for bad in (
        "your-domain.example.com",
        "app.example.com",
        "example.org",
        "render.example.org",
        "app.example.net",
        "app.example.test",
        "oriso.invalid",
        "localhost",
        "app.localhost",
        "127.0.0.1",
        "0.0.0.0",
        "https://dev.oriso.internal",
        "dev.oriso.internal/app",
        "dev.oriso.internal/",
        "dev oriso.internal",
        "oriso..internal",
        "oriso.-dev.internal",
        "oriso-.dev.internal",
        ".oriso.internal",
        "dev.oriso.internal:0",
        "dev.oriso.internal:65536",
        # Ingress host/TLS fields take no port; ports belong in explicit URLs.
        "render.oriso.internal:8443",
    ):
        proc = helm_template("--set-string", f"global.domainName={bad}")
        assert_fails_naming(proc, "global.domainName", f"global.domainName={bad!r}")
    print("PASS: placeholder, scheme, path and whitespace domains fail")


def test_explicit_mail_url_placeholders_fail() -> None:
    for key, bad in (
        ("accountInviteAdminFrontendBaseUrl", "https://your-domain.example.com"),
        ("magicLinkFrontendBaseUrl", "app.oriso.internal"),
        ("passwordResetFrontendBaseUrl", "https://app.oriso.internal/"),
        ("accountInviteAppFrontendBaseUrl", "https://app..oriso.internal"),
        ("accountInviteAppFrontendBaseUrl", "https://app-.oriso.internal"),
        ("magicLinkFrontendBaseUrl", "https://app.oriso.internal:70000"),
        ("magicLinkFrontendBaseUrl", "https://app.example.org"),
        ("accountInviteAdminFrontendBaseUrl", "https://admin.oriso.invalid"),
        ("magicLinkFrontendBaseUrl", "https://localhost"),
        ("passwordResetFrontendBaseUrl", "https://127.0.0.1"),
    ):
        proc = helm_template(
            "--set-string",
            f"global.domainName={VALID_TEST_DOMAIN}",
            "--set-string",
            f"userService.{key}={bad}",
        )
        assert_fails_naming(proc, f"userService.{key}", f"userService.{key}={bad!r}")
    print("PASS: explicit UserService mail URLs are validated, not trusted")


def test_valid_hosts_with_port_and_path_render() -> None:
    proc = helm_template(
        "--set-string",
        "global.domainName=app-1.render.oriso.internal",
        "--set-string",
        "userService.passwordResetAdminFrontendBaseUrl=https://admin.render.oriso.internal:443/admin",
    )
    docs = rendered(proc, "valid host with port")
    data = configmap_data(docs, "userservice-configmap-env")
    # The port and the /admin path must survive into the rendered env, not just
    # pass validation.
    assert (
        data["PASSWORD_RESET_ADMIN_FRONTEND_BASE_URL"]
        == "https://admin.render.oriso.internal:443/admin"
    ), data["PASSWORD_RESET_ADMIN_FRONTEND_BASE_URL"]
    assert data["APP_BASE_URL"] == "https://app-1.render.oriso.internal", data["APP_BASE_URL"]
    print("PASS: valid hyphenated host, explicit URL with port 1-65535 and path render")


def assert_public_urls_match(docs: list[dict], domain: str, explicit: dict, label: str) -> None:
    origin = f"https://{domain}"
    for configmap, keys in PUBLIC_URL_KEYS_BY_CONFIGMAP.items():
        data = configmap_data(docs, configmap)
        for key in keys:
            value = data.get(key)
            assert value, f"{label}: {configmap} must render {key}"
            for marker in PLACEHOLDER_MARKERS:
                assert marker not in value, f"{label}: {configmap}.{key}={value!r} contains {marker!r}"
            if key in explicit:
                assert urlparse(value).scheme == "https", f"{label}: {key}={value!r} is not https"
                assert value == explicit[key], f"{label}: {key}={value!r}, expected {explicit[key]!r}"
                continue
            assert urlparse(value).scheme == "https", (
                f"{label}: {configmap}.{key}={value!r} must use https with TLS on"
            )
            assert urlparse(value).hostname == domain, (
                f"{label}: {configmap}.{key}={value!r} does not point at {domain}"
            )
    keycloak = configmap_data(docs, "keycloak-configmap-env")
    assert keycloak["ORISO_APP_BASE_URL"] == origin, keycloak["ORISO_APP_BASE_URL"]
    assert keycloak["KEYCLOAK_HOSTNAME"] == f"{origin}/auth", keycloak["KEYCLOAK_HOSTNAME"]
    assert synapse_public_baseurl(docs) == f"{origin}/", (
        f"{label}: Synapse public_baseurl must be the public origin, got "
        f"{synapse_public_baseurl(docs)!r}"
    )


def test_committed_overlays_render_with_their_domain() -> None:
    for overlay, expected in OVERLAYS.items():
        docs = rendered(
            helm_template("-f", os.path.join(CHART_DIR, overlay)), overlay
        )
        assert_public_urls_match(docs, expected["domain"], expected["explicit"], overlay)

        missing = set(USERSERVICE_PUBLIC_URL_KEYS) - deployment_env_names(docs, "userservice")
        assert not missing, f"{overlay}: UserService Deployment must import {sorted(missing)}"
        assert "ORISO_APP_BASE_URL" in deployment_env_names(docs, "keycloak"), (
            f"{overlay}: Keycloak Deployment must import ORISO_APP_BASE_URL"
        )
        print(f"PASS: {overlay} renders every public URL on {expected['domain']}")


def test_prod_overlay_requires_domain_at_install() -> None:
    prod = os.path.join(CHART_DIR, PROD_OVERLAY)
    assert_fails_naming(helm_template("-f", prod), "global.domainName", PROD_OVERLAY)

    docs = rendered(
        helm_template("-f", prod, "--set-string", f"global.domainName={VALID_TEST_DOMAIN}"),
        f"{PROD_OVERLAY} + global.domainName",
    )
    assert_public_urls_match(docs, VALID_TEST_DOMAIN, {}, PROD_OVERLAY)
    print(f"PASS: {PROD_OVERLAY} fails without global.domainName and renders with it")


def test_matrix_identity_placeholders_fail() -> None:
    base = ("--set-string", f"global.domainName={VALID_TEST_DOMAIN}")
    for key, bad in (
        ("matrix.matrixServerName", ""),
        ("matrix.matrixServerName", "your-server.local"),
        ("matrix.synapseServerName", "your-server.local"),
        ("global.matrix.matrixServerName", "your-server.local"),
        ("matrixrtcAuth.membershipReaderUserId", "@matrixrtc-auth:your-server.local"),
    ):
        proc = helm_template(*base, "--set-string", f"{key}={bad}")
        assert_fails_naming(proc, key, f"{key}={bad!r}")
    print("PASS: empty or placeholder Matrix identity fails and names the value")


def test_prod_overlay_names_no_oriso_host() -> None:
    with open(os.path.join(CHART_DIR, PROD_OVERLAY), encoding="utf-8") as handle:
        hits = [
            f"{number}: {line.strip()}"
            for number, line in enumerate(handle, 1)
            if "oriso.org" in line.lower()
        ]
    assert not hits, f"{PROD_OVERLAY} must not name an oriso.org host:\n" + "\n".join(hits)
    print(f"PASS: {PROD_OVERLAY} names no oriso.org host")


def test_dev_mail_links_derive_from_domain() -> None:
    docs = rendered(
        helm_template("-f", os.path.join(CHART_DIR, "values-dev.yaml")), "values-dev.yaml"
    )
    data = configmap_data(docs, "userservice-configmap-env")
    # Dev serves App and Admin on one host; the invite builder appends
    # /admin/tenant-onboarding itself, so the admin invite base is the bare origin.
    assert data["ACCOUNT_INVITE_APP_FRONTEND_BASE_URL"] == "https://dev.oriso.org"
    assert data["ACCOUNT_INVITE_ADMIN_FRONTEND_BASE_URL"] == "https://dev.oriso.org"
    assert data["MAGIC_LINK_FRONTEND_BASE_URL"] == "https://dev.oriso.org"
    assert data["PASSWORD_RESET_FRONTEND_BASE_URL"] == "https://dev.oriso.org"
    assert data["INACTIVE_ACCOUNT_NOTIFICATION_APP_BASE_URL"] == "https://dev.oriso.org"
    print("PASS: dev invite and notification links derive from global.domainName")


def test_derived_admin_reset_url_carries_admin_prefix() -> None:
    docs = rendered(
        helm_template("--set-string", f"global.domainName={VALID_TEST_DOMAIN}"),
        "derived defaults",
    )
    data = configmap_data(docs, "userservice-configmap-env")
    assert data["PASSWORD_RESET_ADMIN_FRONTEND_BASE_URL"] == f"https://{VALID_TEST_DOMAIN}/admin"
    assert data["ACCOUNT_INVITE_ADMIN_FRONTEND_BASE_URL"] == f"https://{VALID_TEST_DOMAIN}"
    print("PASS: derived admin reset URL carries /admin, admin invite base does not")


def test_non_tls_flips_the_scheme() -> None:
    docs = rendered(
        helm_template(
            "-f", os.path.join(CHART_DIR, "values-dev.yaml"), "--set", "global.enableTls=false"
        ),
        "values-dev.yaml with enableTls=false",
    )
    keycloak = configmap_data(docs, "keycloak-configmap-env")
    assert keycloak["ORISO_APP_BASE_URL"] == "http://dev.oriso.org", keycloak["ORISO_APP_BASE_URL"]
    assert synapse_public_baseurl(docs) == "http://dev.oriso.org/"
    print("PASS: enableTls=false renders http origins")


def main() -> None:
    test_default_values_fail_naming_domain()
    test_placeholder_or_malformed_domains_fail()
    test_explicit_mail_url_placeholders_fail()
    test_valid_hosts_with_port_and_path_render()
    test_committed_overlays_render_with_their_domain()
    test_prod_overlay_requires_domain_at_install()
    test_prod_overlay_names_no_oriso_host()
    test_matrix_identity_placeholders_fail()
    test_dev_mail_links_derive_from_domain()
    test_derived_admin_reset_url_carries_admin_prefix()
    test_non_tls_flips_the_scheme()


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
