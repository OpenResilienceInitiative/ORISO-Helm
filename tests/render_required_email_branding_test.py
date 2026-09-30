#!/usr/bin/env python3
"""A fresh chart needs its own platform mail name, while ORISO overlays retain theirs."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def render(overlay: str | None = None, brand: str | None = None) -> subprocess.CompletedProcess[str]:
    """Render with valid unrelated requirements so branding is the isolated gate."""
    args = [
        "helm", "template", "email-branding-test", CHART_DIR,
        "-f", os.path.join(CHART_DIR, "values.yaml.default"),
        "-f", os.path.join(CHART_DIR, "tests/fixtures/values-render-domain.yaml"),
        "-f", os.path.join(CHART_DIR, "secrets.yaml.default"),
        "-f", os.path.join(CHART_DIR, "tests/fixtures/render-required-secrets.yaml"),
        "--set-string", "userService.smtpUser=render-only-smtp-user",
        "--set-string", "userService.smtpPassword=render-only-smtp-password",
    ]
    if overlay:
        args.extend(["-f", os.path.join(CHART_DIR, f"values-{overlay}.yaml")])
    if brand is not None:
        args.extend(["--set-string", f"userService.emailBrandingName={brand}"])
    return subprocess.run(args, capture_output=True, text=True, check=False)


def branding_env(result: subprocess.CompletedProcess[str]) -> str:
    """Read the actual UserService value handed to the service process."""
    assert result.returncode == 0, result.stderr
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]
    configmap = next(doc for doc in docs if doc.get("kind") == "ConfigMap"
                     and doc.get("metadata", {}).get("name") == "userservice-configmap-env")
    deployment = next(doc for doc in docs if doc.get("kind") == "Deployment"
                      and doc.get("metadata", {}).get("name") == "userservice")
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    brand = next(entry for entry in env if entry["name"] == "EMAIL_BRANDING_NAME")
    assert brand["valueFrom"]["configMapKeyRef"] == {
        "name": "userservice-configmap-env", "key": "EMAIL_BRANDING_NAME"
    }
    return configmap["data"]["EMAIL_BRANDING_NAME"]


def test_required_email_branding() -> None:
    """Reject missing/blank names and accept explicit names in every environment."""
    base_values = yaml.safe_load((Path(CHART_DIR) / "values.yaml.default").read_text())
    assert base_values["userService"]["emailBrandingName"] == ""
    for overlay in (None, "prod"):
        # The shared render fixture names a test-only platform for the other
        # chart contracts, so explicitly clear it to test the fresh-install gate.
        for brand in ("", "   "):
            result = render(overlay, brand)
            assert result.returncode != 0, (overlay, brand)
            assert "userService.emailBrandingName" in result.stderr, result.stderr

    assert branding_env(render(brand="Community Care")) == "Community Care"
    assert branding_env(render("prod", "Independent Platform")) == "Independent Platform"
    assert branding_env(render("dev")) == "ORISO"
    assert branding_env(render("pre-dev")) == "ORISO"


if __name__ == "__main__":
    try:
        test_required_email_branding()
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
