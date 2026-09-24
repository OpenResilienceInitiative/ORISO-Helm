#!/usr/bin/env python3
"""Render and path-selection guard for the two additive public entry endpoints.

This models ingress-nginx's descending path-length / first-regex-match ordering;
it is not a replacement for inspecting the deployed nginx configuration.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW = "userservice-public-entry-ingress"
OLD = "userservice-rewrite-ingress"
PREFIX = "nginx.ingress.kubernetes.io/"
VALUES = [
    "-f", os.path.join(CHART_DIR, "values.yaml.default"),
    "-f", os.path.join(CHART_DIR, "tests", "fixtures", "values-render-domain.yaml"),
    "-f", os.path.join(CHART_DIR, "tests", "fixtures", "values-public-entry-render.yaml"),
]


def render(chart, *args):
    result = subprocess.run(
        ["helm", "template", "public-entry", chart, *VALUES, *args],
        capture_output=True, text=True, check=True,
    )
    documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    # Prove this exercised the application chart and vendored dependencies.
    assert len(documents) > 100
    assert any(doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "userservice" for doc in documents)
    controller_services = [doc for doc in documents if doc.get("kind") == "Service" and doc["metadata"]["name"] == "ingress-nginx"]
    assert len(controller_services) == 1
    assert controller_services[0]["spec"]["externalTrafficPolicy"] == "Local", "preserve client addresses for rate buckets"
    return {doc["metadata"]["name"]: doc for doc in documents if doc.get("kind") == "Ingress"}


def main():
    with tempfile.TemporaryDirectory(prefix="public-entry-package-") as package_dir:
        chart = CHART_DIR
        subprocess.run(["helm", "lint", chart, *VALUES], check=True, capture_output=True, text=True)
        subprocess.run(["helm", "package", chart, "--destination", package_dir],
                       check=True, capture_output=True, text=True)
        packages = [os.path.join(package_dir, name) for name in os.listdir(package_dir) if name.endswith(".tgz")]
        assert len(packages) == 1
        docs = render(chart)
        assert render(packages[0]) == docs, "packaged chart must retain the same ingress contracts"
        public, legacy = docs[NEW], docs[OLD]
        annotations = public["metadata"]["annotations"]
        assert annotations[PREFIX + "limit-rps"] == "5"
        assert annotations[PREFIX + "limit-burst-multiplier"] == "2"
        assert annotations[PREFIX + "configuration-snippet"].strip() == "limit_req_status 429;"
        assert annotations[PREFIX + "rewrite-target"] == "/$1"
        for key in ("cors-allow-headers", "cors-allow-methods", "enable-cors", "ssl-redirect", "use-regex"):
            assert annotations[PREFIX + key] == legacy["metadata"]["annotations"][PREFIX + key]
        assert public["spec"]["ingressClassName"] == legacy["spec"]["ingressClassName"]
        assert public["spec"]["rules"][0]["host"] == legacy["spec"]["rules"][0]["host"]
        assert public["spec"].get("tls") == legacy["spec"].get("tls")
        route = public["spec"]["rules"][0]["http"]["paths"][0]
        broad = legacy["spec"]["rules"][0]["http"]["paths"][0]
        assert route["backend"] == broad["backend"]
        assert route["pathType"] == "ImplementationSpecific"
        assert len(route["path"]) > len(broad["path"]), "nginx sorts regex paths by descending length"
        # All same-host ingresses become regex locations when rewrite/use-regex is enabled.
        # Check selection against the complete chart, including main-ingress's broad prefix.
        host = public["spec"]["rules"][0]["host"]
        routes = sorted(
            [(path["path"], name)
             for name, ingress in docs.items()
             for rule in ingress.get("spec", {}).get("rules", [])
             if rule.get("host") == host
             for path in rule.get("http", {}).get("paths", [])],
            reverse=True, key=lambda pair: len(pair[0]),
        )
        for uri in ("/service/users/identity-suggestions", "/service/users/invitelinks/abc_-123/context"):
            selected = next(name for pattern, name in routes if re.match(pattern, uri, re.I))
            assert selected == NEW
            match = re.match(route["path"], uri)
            assert "/" + match.group(1) == uri.removeprefix("/service")
        for uri in (
            "/service/users/invitelinks/token/redeem", "/service/users/login",
            "/service/users/identity-suggestions-extra", "/service/users/identity-suggestions/extra",
            "/service/users/invitelinks//context", "/service/users/invitelinks/a/b/context",
            "/service/users/invitelinks/token/context-extra", "/service/users/invitelinks/token/context/extra",
            "/users/identity-suggestions", "/users/invitelinks/token/context",
            "/service/useradmin/invitelinks", "/service/conversations",
        ):
            assert re.match(route["path"], uri, re.I) is None, uri
        assert not any(key.startswith(PREFIX + "limit-") for key in legacy["metadata"]["annotations"])
        overrides = render(chart, "--set", "userService.publicEntryRateLimit.requestsPerSecond=9",
                           "--set", "userService.publicEntryRateLimit.burstMultiplier=3")
        changed = overrides[NEW]["metadata"]["annotations"]
        assert changed[PREFIX + "limit-rps"] == "9"
        assert changed[PREFIX + "limit-burst-multiplier"] == "3"
        assert overrides[OLD] == legacy
    print("PASS: full chart lint, render, package round-trip, rate limits, overrides, and sibling isolation")


if __name__ == "__main__":
    main()
