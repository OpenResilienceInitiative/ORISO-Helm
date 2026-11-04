#!/usr/bin/env python3
"""Render guard: ClickHouse must not write system.metric_log."""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DOMAIN = "render.oriso.internal"
SYSTEM_LOG_FILE = "config.d/system_log.xml"
HELM_TIMEOUT_SECONDS = 180


def render() -> list[dict]:
    command = [
        "helm",
        "template",
        "caritas",
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
        "--set-string",
        "userService.smtpUser=smtp-canary-user",
        "--set-string",
        "userService.smtpPassword=smtp-canary-password",
        "--set",
        "signoz.enabled=true",
        "--set-string",
        f"signoz.signoz.env.signoz_global_external__url=https://{FIXTURE_DOMAIN}/signoz",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=HELM_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise AssertionError(f"helm template timed out after {HELM_TIMEOUT_SECONDS}s") from error
    if result.returncode:
        raise AssertionError(result.stderr)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def main() -> int:
    installations = [
        document for document in render() if document.get("kind") == "ClickHouseInstallation"
    ]
    if len(installations) != 1:
        print(f"expected one ClickHouseInstallation, found {len(installations)}", file=sys.stderr)
        return 1
    files = installations[0]["spec"]["configuration"]["files"]
    # Parse the XML so a commented-out element cannot satisfy the check.
    root = ET.fromstring(files.get(SYSTEM_LOG_FILE, "<clickhouse/>"))
    disabled = [element for element in root.findall("metric_log") if element.get("remove") == "1"]
    if len(disabled) != 1:
        print(f'{SYSTEM_LOG_FILE} must contain <metric_log remove="1"/>', file=sys.stderr)
        return 1
    print("clickhouse system logs ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
