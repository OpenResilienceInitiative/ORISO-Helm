#!/usr/bin/env python3
"""Render guard: Dev ClickHouse must not write system.metric_log or system.trace_log."""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DOMAIN = "render.oriso.internal"
DEV_METRIC_LOG_FILE = "config.d/99-oriso-disable-metric-log.xml"
DEV_TRACE_LOG_FILE = "config.d/99-oriso-disable-trace-log.xml"
HELM_TIMEOUT_SECONDS = 180


def render(*overlays: str) -> list[dict]:
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
        *[
            item
            for overlay in overlays
            for item in ("-f", os.path.join(CHART_DIR, overlay))
        ],
        "--set-string",
        "global.secrets.redisdefaultPass=test-redis-password",
        "--set-string",
        "userService.smtpUser=smtp-canary-user",
        "--set-string",
        "userService.smtpPassword=smtp-canary-password",
        "--set",
        "signoz.enabled=true",
    ]
    if not overlays:
        command.extend([
            "--set-string",
            f"signoz.signoz.env.signoz_global_external__url=https://{FIXTURE_DOMAIN}/signoz",
        ])
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
        document for document in render("values-dev.yaml") if document.get("kind") == "ClickHouseInstallation"
    ]
    if len(installations) != 1:
        print(f"expected one ClickHouseInstallation, found {len(installations)}", file=sys.stderr)
        return 1
    files = installations[0]["spec"]["configuration"]["files"]
    # Parse the XML so a commented-out element cannot satisfy the check.
    dev_log_files = ((DEV_METRIC_LOG_FILE, "metric_log"), (DEV_TRACE_LOG_FILE, "trace_log"))
    for file_name, element in dev_log_files:
        root = ET.fromstring(files.get(file_name, "<clickhouse/>"))
        found = root.findall(element)
        if len(found) != 1 or found[0].get("remove") != "1":
            print(f'{file_name} must contain <{element} remove="1"/>', file=sys.stderr)
            return 1
    # The default chart must keep writing it: removal is Dev-only.
    default = [d for d in render() if d.get("kind") == "ClickHouseInstallation"]
    if len(default) != 1:
        print(f"expected one default ClickHouseInstallation, found {len(default)}", file=sys.stderr)
        return 1
    if any(file_name in default[0]["spec"]["configuration"]["files"] for file_name, _ in dev_log_files):
        print("Dev writer removals must be absent from the default chart", file=sys.stderr)
        return 1

    # Pre-Dev uses the Dev overlay first. Inspect that actual installation.
    predev = [
        d for d in render("values-dev.yaml", "values-pre-dev.yaml")
        if d.get("kind") == "ClickHouseInstallation"
    ]
    if len(predev) != 1:
        print(f"expected one Pre-Dev ClickHouseInstallation, found {len(predev)}", file=sys.stderr)
        return 1
    predev_files = predev[0]["spec"]["configuration"]["files"]
    for file_name, _ in dev_log_files:
        content = predev_files.get(file_name, "<clickhouse/>")
        if not isinstance(content, str):
            print(f"{file_name} must be valid XML in Pre-Dev", file=sys.stderr)
            return 1
        root = ET.fromstring(content)
        if root.tag != "clickhouse" or root.attrib or len(root):
            print(f"{file_name} must leave the Pre-Dev writer configuration intact", file=sys.stderr)
            return 1
    print("Dev ClickHouse system logs ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
