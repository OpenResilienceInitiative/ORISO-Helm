#!/usr/bin/env python3
"""Render guard: all browser Matrix logins share one client address, so the
Synapse login limit must not fall back to the shipped burst of 5. Wrong-password
protection (failed_attempts) must stay at the Synapse default."""

from __future__ import annotations

import os
import re
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELM_TIMEOUT_SECONDS = 180
INGRESS_NAME = "matrix-login-rate-limit-ingress"
LOGIN_PATHS = {
    "/_matrix/client/v3/login": True,
    "/_matrix/client/r0/login/": True,
    "/_matrix/client/api/v1/login": True,
    "/_matrix/client/v3/login;x=1": True,
    "/_matrix/client/v3/login/sso/redirect": False,
    "/_matrix/client/v3/sync": False,
}
EXPECTED = {
    "address": {"per_second": 1, "burst_count": 50},
    "account": {"per_second": 0.1, "burst_count": 20},
}


def render_documents(*extra: str) -> list[dict]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "synapse-login-limit",
            CHART_DIR,
            "-f",
            os.path.join(CHART_DIR, "values.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "tests", "fixtures", "values-render-domain.yaml"),
            "-f",
            os.path.join(CHART_DIR, "secrets.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "tests", "fixtures", "render-required-secrets.yaml"),
            "--set-string",
            "global.secrets.redisdefaultPass=test-redis-password",
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=HELM_TIMEOUT_SECONDS,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def render_login_limit(*extra: str) -> dict:
    for document in render_documents(*extra):
        for value in (document.get("data") or {}).values():
            if not isinstance(value, str):
                continue
            if "server_name:" in value and "rc_login" in value:
                return yaml.safe_load(value)["rc_login"]
    raise AssertionError("no Synapse config with rc_login rendered")


def main() -> int:
    cases = {
        "default values": ([], EXPECTED),
        # A deployment values file without the new keys must not lose the limit.
        "values without rateLimits": (["--set", "matrix.rateLimits=null"], EXPECTED),
        "override": (
            ["--set", "matrix.rateLimits.loginAddress.burstCount=7"],
            {**EXPECTED, "address": {"per_second": 1, "burst_count": 7}},
        ),
    }
    for name, (extra, expected) in cases.items():
        actual = render_login_limit(*extra)
        if actual != expected:
            print(f"{name}: expected rc_login {expected}, got {actual}", file=sys.stderr)
            return 1
    ingresses = [
        document
        for document in render_documents()
        if document.get("metadata", {}).get("name") == INGRESS_NAME
    ]
    if len(ingresses) != 1:
        print(f"expected one {INGRESS_NAME}, found {len(ingresses)}", file=sys.stderr)
        return 1
    annotations = ingresses[0]["metadata"]["annotations"]
    path = ingresses[0]["spec"]["rules"][0]["http"]["paths"][0]["path"]
    if annotations.get("nginx.ingress.kubernetes.io/limit-rpm") != "6":
        print("public login path must be limited per IP", file=sys.stderr)
        return 1
    for uri, expected in LOGIN_PATHS.items():
        if bool(re.match(path, uri)) != expected:
            print(f"{uri}: expected match {expected} for {path}", file=sys.stderr)
            return 1
    if any(
        document.get("metadata", {}).get("name") == INGRESS_NAME
        for document in render_documents("--set", "matrix.loginRateLimit.enabled=false")
    ):
        print("loginRateLimit.enabled=false must remove the ingress", file=sys.stderr)
        return 1
    print("Synapse login rate limit ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
