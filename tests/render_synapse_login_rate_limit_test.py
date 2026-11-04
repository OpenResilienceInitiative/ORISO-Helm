#!/usr/bin/env python3
"""Render guard: all browser Matrix logins share one client address, so the
Synapse login limit must not fall back to the shipped burst of 5. Wrong-password
protection (failed_attempts) must stay at the Synapse default."""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELM_TIMEOUT_SECONDS = 180
EXPECTED = {
    "address": {"per_second": 1, "burst_count": 50},
    "account": {"per_second": 0.1, "burst_count": 20},
}


def render_login_limit(*extra: str) -> dict:
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
    for document in yaml.safe_load_all(result.stdout):
        for value in ((document or {}).get("data") or {}).values():
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
    print("Synapse login rate limit ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
