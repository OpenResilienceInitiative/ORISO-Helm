#!/usr/bin/env python3
"""Render and path-selection guard for the per-IP limit on Keycloak's token endpoint (#1338).

The limit lives on its own Ingress so the broad `/auth` route in main-ingress stays unlimited.
This models ingress-nginx's descending path-length / first-regex-match ordering; it is not a
replacement for inspecting the deployed nginx configuration.
"""
from __future__ import annotations

import os
import re
import subprocess

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW = "keycloak-token-rate-limit-ingress"
MAIN = "main-ingress"
PREFIX = "nginx.ingress.kubernetes.io/"
VALUES = [
    "-f", os.path.join(CHART_DIR, "values.yaml.default"),
    "-f", os.path.join(CHART_DIR, "tests", "fixtures", "values-render-domain.yaml"),
    "-f", os.path.join(CHART_DIR, "tests", "fixtures", "values-public-entry-render.yaml"),
]
PRIVATE_RANGES = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8"
TEMPLATE = os.path.join(CHART_DIR, "templates", "nginx", "keycloak-token-rate-limit-ingress.yaml")
ENABLED = ("--set", "global.keycloak.tokenRateLimit.enabled=true")


def render(*args):
    result = subprocess.run(
        ["helm", "template", "token-limit", CHART_DIR, *VALUES, *args],
        capture_output=True, text=True, check=True,
    )
    documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert any(doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "keycloak"
               for doc in documents), "render must include the keycloak subchart"
    return {doc["metadata"]["name"]: doc for doc in documents if doc.get("kind") == "Ingress"}


def selected_ingress(docs, host, uri):
    routes = sorted(
        [(path["path"], name)
         for name, ingress in docs.items()
         for rule in ingress.get("spec", {}).get("rules", [])
         if rule.get("host") == host
         for path in rule.get("http", {}).get("paths", [])],
        reverse=True, key=lambda pair: len(pair[0]),
    )
    # ingress-nginx renders regex locations as `~* "^<path>"`
    return next(name for pattern, name in routes if re.match(pattern, uri, re.I))


def main():
    # Off by default: on Dev ingress-nginx sees the node's address, not the client's, so one
    # shared bucket would throttle every external user (review on ORISO-Helm#401).
    default = render()
    assert NEW not in default, "the limit must ship switched off"
    no_block = render("--set", "global.keycloak.tokenRateLimit=null")
    assert NEW not in no_block, "a missing tokenRateLimit block must mean off"
    # Exempt ranges come from values only, never from a fallback in the template.
    with open(TEMPLATE, encoding="utf-8") as handle:
        assert not re.search(r"\d+\.\d+\.\d+\.\d+/\d+", handle.read()), "no CIDR literal in the template"

    docs = render(*ENABLED)
    limited, main_ingress = docs[NEW], docs[MAIN]
    assert default[MAIN] == main_ingress

    annotations = limited["metadata"]["annotations"]
    assert annotations[PREFIX + "limit-rps"] == "5"
    assert annotations[PREFIX + "limit-burst-multiplier"] == "5"
    assert annotations[PREFIX + "configuration-snippet"].strip() == "limit_req_status 429;"
    assert annotations[PREFIX + "limit-whitelist"] == PRIVATE_RANGES
    assert annotations[PREFIX + "use-regex"] == "true"
    assert annotations[PREFIX + "ssl-redirect"] == main_ingress["metadata"]["annotations"][PREFIX + "ssl-redirect"]
    assert PREFIX + "rewrite-target" not in annotations, "Keycloak must see the original path"
    assert limited["spec"]["ingressClassName"] == main_ingress["spec"]["ingressClassName"]
    host = main_ingress["spec"]["rules"][0]["host"]
    assert limited["spec"]["rules"][0]["host"] == host

    route = limited["spec"]["rules"][0]["http"]["paths"][0]
    keycloak_route = next(path for path in main_ingress["spec"]["rules"][0]["http"]["paths"]
                          if path["path"] == "/auth")
    assert route["backend"] == keycloak_route["backend"]
    assert route["pathType"] == "ImplementationSpecific"

    for uri in (
        "/auth/realms/online-beratung/protocol/openid-connect/token",
        "/auth/realms/online-beratung/protocol/openid-connect/token/",
        "/auth/realms/online-beratung/protocol/openid-connect/token;jsessionid=x",
        "/auth/realms/master/protocol/openid-connect/token",
    ):
        assert selected_ingress(docs, host, uri) == NEW, uri
    for uri in (
        "/auth",
        "/auth/realms/online-beratung/protocol/openid-connect/token/introspect",
        "/auth/realms/online-beratung/protocol/openid-connect/tokens",
        "/auth/realms/online-beratung/protocol/openid-connect/certs",
        "/auth/realms/online-beratung/protocol/openid-connect/auth",
        "/auth/realms/online-beratung/protocol/openid-connect/logout",
        "/auth/realms/online-beratung/login-actions/authenticate",
        "/auth/realms/online-beratung/otp-config/send-verification-mail/someone",
        "/auth/realms/a/b/protocol/openid-connect/token",
        "/realms/online-beratung/protocol/openid-connect/token",
    ):
        assert selected_ingress(docs, host, uri) != NEW, uri

    assert not any(key.startswith(PREFIX + "limit-") for key in main_ingress["metadata"]["annotations"]), \
        "the broad /auth route must stay unlimited"

    overrides = render(
        *ENABLED,
        "--set", "global.keycloak.tokenRateLimit.requestsPerSecond=9",
        "--set", "global.keycloak.tokenRateLimit.burstMultiplier=3",
        "--set-string", "global.keycloak.tokenRateLimit.exemptCidrs=10.42.0.0/16\\,10.43.0.0/16",
    )
    changed = overrides[NEW]["metadata"]["annotations"]
    assert changed[PREFIX + "limit-rps"] == "9"
    assert changed[PREFIX + "limit-burst-multiplier"] == "3"
    assert changed[PREFIX + "limit-whitelist"] == "10.42.0.0/16,10.43.0.0/16"
    assert overrides[MAIN] == main_ingress

    no_exemptions = render(*ENABLED, "--set-string", "global.keycloak.tokenRateLimit.exemptCidrs=")
    assert PREFIX + "limit-whitelist" not in no_exemptions[NEW]["metadata"]["annotations"]

    disabled = render("--set", "global.keycloak.tokenRateLimit.enabled=false")
    assert NEW not in disabled
    assert disabled[MAIN] == main_ingress
    print("PASS: token endpoint limit is off by default, renders when enabled, selects only the "
          "token path, honours overrides, takes exemptions from values only, and leaves /auth unlimited")


if __name__ == "__main__":
    main()
