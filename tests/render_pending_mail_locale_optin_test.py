#!/usr/bin/env python3
"""Keep unreviewed mail copy opt-in on Dev and off elsewhere."""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "EMAIL_ALLOW_UNREVIEWED_LOCALES"


def render(overlay: str | None) -> tuple[dict, dict]:
    args = [
        "helm", "template", "mail-locale-gate-test", CHART_DIR,
        "-f", os.path.join(CHART_DIR, "values.yaml.default"),
        "-f", os.path.join(CHART_DIR, "tests/fixtures/values-render-domain.yaml"),
        "-f", os.path.join(CHART_DIR, "secrets.yaml.default"),
        "-f", os.path.join(CHART_DIR, "tests/fixtures/render-required-secrets.yaml"),
    ]
    if overlay:
        args += ["-f", os.path.join(CHART_DIR, f"values-{overlay}.yaml")]
    args += [
        "--set-string", "userService.smtpUser=render-only-smtp-user",
        "--set-string", "userService.smtpPassword=render-only-smtp-password",
    ]
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    docs = [doc for doc in yaml.safe_load_all(proc.stdout) if isinstance(doc, dict)]
    configmap = next(doc for doc in docs if doc.get("kind") == "ConfigMap"
                     and doc.get("metadata", {}).get("name") == "userservice-configmap-env")
    deployment = next(doc for doc in docs if doc.get("kind") == "Deployment"
                      and doc.get("metadata", {}).get("name") == "userservice")
    return configmap, deployment


def test_pending_mail_locales_are_dev_only() -> None:
    for overlay, expected in ((None, "false"), ("dev", "true"),
                              ("pre-dev", "false"), ("prod", "false")):
        configmap, deployment = render(overlay)
        assert configmap["data"].get(KEY) == expected, overlay
        env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        flag = next((entry for entry in env if entry["name"] == KEY), None)
        assert flag is not None, overlay
        assert flag["valueFrom"]["configMapKeyRef"] == {
            "name": "userservice-configmap-env", "key": KEY,
        }, overlay


if __name__ == "__main__":
    try:
        test_pending_mail_locales_are_dev_only()
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
