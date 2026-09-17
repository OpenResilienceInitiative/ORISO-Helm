#!/usr/bin/env python3
"""Render and path-selection guard for the two additive public entry endpoints.

This models ingress-nginx's descending path-length / first-regex-match ordering;
it is not a replacement for inspecting the deployed nginx configuration.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW = "userservice-public-entry-ingress"
OLD = "userservice-rewrite-ingress"
PREFIX = "nginx.ingress.kubernetes.io/"


def render(chart, *args):
    result = subprocess.run(
        ["helm", "template", "public-entry", chart, *args],
        capture_output=True, text=True, check=True,
    )
    return {doc["metadata"]["name"]: doc for doc in yaml.safe_load_all(result.stdout) if doc}


def main():
    with tempfile.TemporaryDirectory(prefix="public-entry-ingress-") as chart:
        os.makedirs(os.path.join(chart, "templates"))
        with open(os.path.join(chart, "Chart.yaml"), "w") as file:
            file.write("apiVersion: v2\nname: public-entry-test\nversion: 0.0.0\n")
        shutil.copyfile(os.path.join(CHART_DIR, "values.yaml.default"), os.path.join(chart, "values.yaml"))
        for name in (NEW, OLD):
            shutil.copyfile(os.path.join(CHART_DIR, "templates", "nginx", name + ".yaml"),
                            os.path.join(chart, "templates", name + ".yaml"))
        docs = render(chart)
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
        routes = sorted([(route["path"], NEW), (broad["path"], OLD)], reverse=True, key=lambda pair: len(pair[0]))
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
    print("PASS: public entry rate limits, overrides, rewrites, and sibling route isolation")


if __name__ == "__main__":
    main()
