#!/usr/bin/env python3
"""Render guard for ORISO-Helm#628: Synapse delete_stale_devices_after is off
by default and 7d on Dev, and the one-off cleanup script selects only stale
devices and never the operator's own."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELM_TIMEOUT_SECONDS = 180
DAY_MS = 24 * 60 * 60 * 1000


def homeserver(*extra: str) -> dict:
    result = subprocess.run(
        [
            "helm",
            "template",
            "synapse-stale-devices",
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
        if document and document.get("kind") == "ConfigMap" and "homeserver.yaml" in (
            document.get("data") or {}
        ):
            return yaml.safe_load(document["data"]["homeserver.yaml"])
    raise AssertionError("homeserver.yaml ConfigMap not rendered")


def load_cleanup_script():
    path = os.path.join(CHART_DIR, "scripts", "cleanup-stale-matrix-devices.py")
    spec = importlib.util.spec_from_file_location("cleanup_stale_matrix_devices", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    assert "delete_stale_devices_after" not in homeserver()
    dev = homeserver("-f", os.path.join(CHART_DIR, "values-dev.yaml"))
    assert dev["delete_stale_devices_after"] == "7d", dev.get("delete_stale_devices_after")

    cleanup = load_cleanup_script()
    now = 100 * DAY_MS
    devices = [
        {"device_id": "fresh", "last_seen_ts": now - 2 * DAY_MS},
        {"device_id": "old", "last_seen_ts": now - 40 * DAY_MS},
        {"device_id": "never", "last_seen_ts": None},
    ]
    select = cleanup.select_stale_devices
    assert select(devices, now_ms=now, older_than_days=30, include_never_seen=False,
                  delete_all_but=None) == ["old"]
    assert select(devices, now_ms=now, older_than_days=30, include_never_seen=True,
                  delete_all_but=None) == ["old", "never"]
    assert select(devices, now_ms=now, older_than_days=30, include_never_seen=False,
                  delete_all_but="fresh") == ["old", "never"]

    print("synapse stale devices render: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
