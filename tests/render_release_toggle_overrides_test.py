#!/usr/bin/env python3
"""Frontend#1979: the release toggle override reaches CTS on dev only.

`enableNewNotifications` is stored in CTS MongoDB and had no declarative
per-environment switch. CTS now layers RELEASE_TOGGLE_OVERRIDES over the
stored toggles. Dev sets it; Pre-Dev (an overlay on Dev) clears it again, and
the base values and prod leave it empty, so they keep whatever is stored.
"""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIGMAP = "consultingtypeservice-configmap-env"
KEY = "RELEASE_TOGGLE_OVERRIDES"


def render(overlay: str | None = None) -> list[dict]:
    """Render like the sibling contracts: environment overlays come after the
    shared render-domain fixture, so each overlay's own host and SigNoz URL
    stay consistent. Pre-Dev is deployed as an overlay on top of Dev."""
    command = [
        "helm", "template", "release-toggle-overrides-test", CHART_DIR,
        "-f", os.path.join(CHART_DIR, "values.yaml.default"),
        "-f", os.path.join(CHART_DIR, "tests/fixtures/values-render-domain.yaml"),
        "-f", os.path.join(CHART_DIR, "secrets.yaml.default"),
        "-f", os.path.join(CHART_DIR, "tests/fixtures/render-required-secrets.yaml"),
    ]
    if overlay == "pre-dev":
        command += ["-f", os.path.join(CHART_DIR, "values-dev.yaml")]
    if overlay:
        command += ["-f", os.path.join(CHART_DIR, f"values-{overlay}.yaml")]
    command += [
        "--set-string", "userService.smtpUser=smtp-canary-user",
        "--set-string", "userService.smtpPassword=smtp-canary-password",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def override(documents: list[dict]) -> str:
    configmap = next(
        doc for doc in documents
        if doc.get("kind") == "ConfigMap"
        and doc.get("metadata", {}).get("name") == CONFIGMAP
    )
    deployment = next(
        doc for doc in documents
        if doc.get("kind") == "Deployment"
        and doc.get("metadata", {}).get("name") == "consultingtypeservice"
    )
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    wired = next(item for item in env if item["name"] == KEY)
    assert wired["valueFrom"]["configMapKeyRef"] == {"name": CONFIGMAP, "key": KEY}
    return configmap["data"][KEY]


def test_override_is_empty_by_default_and_in_prod() -> None:
    assert override(render()) == ""
    assert override(render("prod")) == ""


def test_dev_switches_new_notifications_on() -> None:
    assert override(render("dev")) == "enableNewNotifications=true"


def test_pre_dev_resets_the_dev_override() -> None:
    # values-pre-dev.yaml is layered on values-dev.yaml, so it must clear the
    # Dev switch explicitly to keep the decision "Dev only".
    assert override(render("pre-dev")) == ""


if __name__ == "__main__":
    try:
        test_override_is_empty_by_default_and_in_prod()
        test_dev_switches_new_notifications_on()
        test_pre_dev_resets_the_dev_override()
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
    print("PASS: release toggle overrides reach CTS on Dev only")
