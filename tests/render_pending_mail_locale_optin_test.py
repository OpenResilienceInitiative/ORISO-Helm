#!/usr/bin/env python3
"""Keep unreviewed mail copy opt-in on Dev and off elsewhere."""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "EMAIL_ALLOW_UNREVIEWED_LOCALES"


def render(overlay: str | None, opt_in: bool | str | None = None) -> tuple[dict, dict]:
    """Render the base chart with one optional environment overlay."""
    args = [
        "helm", "template", "mail-locale-gate-test", CHART_DIR,
        "-f", os.path.join(CHART_DIR, "values.yaml.default"),
        "-f", os.path.join(CHART_DIR, "tests/fixtures/values-render-domain.yaml"),
        "-f", os.path.join(CHART_DIR, "secrets.yaml.default"),
        "-f", os.path.join(CHART_DIR, "tests/fixtures/render-required-secrets.yaml"),
    ]
    if overlay:
        args += ["-f", os.path.join(CHART_DIR, f"values-{overlay}.yaml")]
    if opt_in is not None:
        value = opt_in if isinstance(opt_in, str) else str(opt_in).lower()
        args += ["--set", f"userService.emailAllowUnreviewedLocales={value}"]
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
    """Only Dev opts in, and the Pod template records each value for rollout."""
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
        assert deployment["spec"]["template"]["metadata"]["annotations"][
            "oriso.org/email-allow-unreviewed-locales"
        ] == expected, overlay


def test_dev_opt_out_updates_the_pod_template() -> None:
    """Disabling Dev's exception changes runtime configuration and rollout identity."""
    enabled_config, enabled_deployment = render("dev")
    disabled_config, disabled_deployment = render("dev", opt_in=False)
    assert enabled_config["data"][KEY] == "true"
    assert disabled_config["data"][KEY] == "false"
    assert disabled_config["data"]["EMAIL_BRANDING_NAME"] == "ORISO"
    enabled_annotations = enabled_deployment["spec"]["template"]["metadata"]["annotations"]
    disabled_annotations = disabled_deployment["spec"]["template"]["metadata"]["annotations"]
    assert enabled_annotations["oriso.org/email-allow-unreviewed-locales"] == "true"
    assert disabled_annotations["oriso.org/email-allow-unreviewed-locales"] == "false"


def test_missing_value_defaults_to_false() -> None:
    """Installation values without the key must render false, not an empty value."""
    configmap, deployment = render(None, opt_in="null")
    assert configmap["data"][KEY] == "false"
    assert deployment["spec"]["template"]["metadata"]["annotations"][
        "oriso.org/email-allow-unreviewed-locales"
    ] == "false"


if __name__ == "__main__":
    try:
        test_pending_mail_locales_are_dev_only()
        test_dev_opt_out_updates_the_pod_template()
        test_missing_value_defaults_to_false()
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
