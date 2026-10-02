#!/usr/bin/env python3
"""Brute-force protection for the online-beratung realm (ORISO-UserService#1338, slice 5).

Two halves must ship together:
- realm.json switches protection on with temporary lockout only (fresh imports);
- the Keycloak pod allows concurrent logins per user. Keycloak's default protector
  otherwise answers a second, parallel login of the same user with "Invalid user
  credentials" while the first one runs, and every backend signs in as `technical`.

The ORISO-Keycloak repository carries the same realm values and asserts them too.
"""
from __future__ import annotations

import json
import os
import subprocess

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REALM = os.path.join(CHART_DIR, "charts", "keycloak", "keycloak-resources", "realm.json")
VALUES = [
    "-f", os.path.join(CHART_DIR, "values.yaml.default"),
    "-f", os.path.join(CHART_DIR, "tests", "fixtures", "values-render-domain.yaml"),
    "-f", os.path.join(CHART_DIR, "tests", "fixtures", "values-public-entry-render.yaml"),
]
CONCURRENCY_ENV = "KC_SPI_BRUTE_FORCE_PROTECTOR__DEFAULT_BRUTE_FORCE_DETECTOR__ALLOW_CONCURRENT_REQUESTS"


def keycloak_env(*args):
    result = subprocess.run(
        ["helm", "template", "brute-force", CHART_DIR, *VALUES, *args],
        capture_output=True, text=True, check=True,
    )
    deployment = next(doc for doc in yaml.safe_load_all(result.stdout)
                      if doc and doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "keycloak")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {item["name"]: item.get("value") for item in container.get("env", [])}


def main():
    with open(REALM, encoding="utf-8") as handle:
        realm = json.load(handle)
    assert realm["bruteForceProtected"] is True
    assert realm["permanentLockout"] is False, "never lock anyone out for good"
    assert realm["maxTemporaryLockouts"] == 0
    assert realm["bruteForceStrategy"] == "MULTIPLE"
    # 1 code challenge + 5 resends + 3 wrong codes = 9 counted failures in a legitimate e-mail sign-in
    assert realm["failureFactor"] == 15 and realm["failureFactor"] > 1 + 5 + 3
    assert realm["waitIncrementSeconds"] == 60
    assert realm["maxFailureWaitSeconds"] == 900
    assert realm["maxDeltaTimeSeconds"] == 43200
    assert realm["quickLoginCheckMilliSeconds"] == 1000
    assert realm["minimumQuickLoginWaitSeconds"] == 5

    env = keycloak_env()
    assert env.get(CONCURRENCY_ENV) == "true", "parallel technical-user logins must not lock each other out"
    switched_off = keycloak_env("--set", "online-counseling-keycloak.bruteForce.allowConcurrentRequests=false")
    assert switched_off.get(CONCURRENCY_ENV) == "false"
    print("PASS: realm brute-force values and the concurrent-login setting of the Keycloak pod")


if __name__ == "__main__":
    main()
