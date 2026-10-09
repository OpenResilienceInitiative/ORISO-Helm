#!/usr/bin/env python3
"""Brute-force protection for the online-beratung realm (ORISO-UserService#1338, slice 5).

Two halves must ship together:
- realm.json switches protection on with temporary lockout only (fresh imports);
- the Keycloak pod allows concurrent logins per user. Keycloak's default protector
  serializes same-user requests. Backends must use client credentials before activation.

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
    assert realm["maxSecondaryAuthFailures"] == 0
    assert realm["bruteForceStrategy"] == "MULTIPLE"
    # Pending OTP/mail-cap/delivery responses must not count; the SPI integration test pins that.
    assert realm["failureFactor"] == 15
    assert realm["waitIncrementSeconds"] == 60
    assert realm["maxFailureWaitSeconds"] == 900
    assert realm["maxDeltaTimeSeconds"] == 43200
    assert realm["quickLoginCheckMilliSeconds"] == 1000
    assert realm["minimumQuickLoginWaitSeconds"] == 5

    env = keycloak_env()
    assert env.get(CONCURRENCY_ENV) == "false", "human OTP requests keep Keycloak concurrency protection"
    override = keycloak_env("--set", "online-counseling-keycloak.bruteForce.allowConcurrentRequests=true")
    assert override.get(CONCURRENCY_ENV) == "true"
    print("PASS: realm brute-force values and the concurrent-login setting of the Keycloak pod")


if __name__ == "__main__":
    main()
