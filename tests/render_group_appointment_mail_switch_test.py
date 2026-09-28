#!/usr/bin/env python3
"""Verify the self-help appointment sender is opt-in and reaches UserService."""

from __future__ import annotations

import os
import subprocess

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def render(enabled: str | None = None) -> list[dict]:
    command = [
        "helm", "template", "group-appointment-mail-test", CHART_DIR,
        "-f", os.path.join(CHART_DIR, "values.yaml.default"),
        "-f", os.path.join(CHART_DIR, "tests/fixtures/values-render-domain.yaml"),
        "-f", os.path.join(CHART_DIR, "secrets.yaml.default"),
        "-f", os.path.join(CHART_DIR, "tests/fixtures/render-required-secrets.yaml"),
        "--set-string", "userService.smtpUser=smtp-canary-user",
        "--set-string", "userService.smtpPassword=smtp-canary-password",
    ]
    if enabled is not None:
        command += ["--set-string", f"userService.groupAppointmentMail.enabled={enabled}"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def test_group_appointment_mail_is_disabled_by_default_and_wired() -> None:
    for requested, expected in ((None, "false"), ("true", "true")):
        documents = render(requested)
        configmap = next(
            doc for doc in documents
            if doc.get("kind") == "ConfigMap"
            and doc.get("metadata", {}).get("name") == "userservice-configmap-env"
        )
        assert configmap["data"]["GROUP_APPOINTMENT_MAIL_ENABLED"] == expected
        deployment = next(
            doc for doc in documents
            if doc.get("kind") == "Deployment"
            and "userservice" in doc.get("metadata", {}).get("name", "")
        )
        env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        switch = next(item for item in env if item["name"] == "GROUP_APPOINTMENT_MAIL_ENABLED")
        assert switch["valueFrom"]["configMapKeyRef"] == {
            "name": "userservice-configmap-env",
            "key": "GROUP_APPOINTMENT_MAIL_ENABLED",
        }
