#!/usr/bin/env python3
"""Helm render contract for managed SigNoz observability resources."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
from typing import Any

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def render_result(
    *,
    enabled: bool,
    overlay: str = "values-pre-dev.yaml",
    signoz_enabled: bool = True,
    infra_enabled: bool = True,
    extra_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    command = [
        "helm",
        "template",
        "caritas",
        CHART_DIR,
        "--namespace",
        "caritas",
        "-f",
        os.path.join(CHART_DIR, "values.yaml.default"),
        "-f",
        os.path.join(CHART_DIR, "secrets.yaml.default"),
        "-f",
        os.path.join(CHART_DIR, overlay),
        "--set-string",
        "global.secrets.redisdefaultPass=test-redis-password",
        "--set-string",
        "userService.smtpUser=smtp-test-user",
        "--set-string",
        "userService.smtpPassword=smtp-test-password",
        "--set-string",
        "signoz.signoz.env.signoz_global_external__url=https://your-domain.example.com/signoz",
        "--set",
        f"signoz.orisoObservability.enabled={'true' if enabled else 'false'}",
        "--set",
        f"signoz.enabled={'true' if signoz_enabled else 'false'}",
        "--set",
        f"k8s-infra.enabled={'true' if infra_enabled else 'false'}",
        *extra_args,
    ]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def render(
    *, enabled: bool, overlay: str = "values-pre-dev.yaml"
) -> list[dict[str, Any]]:
    result = render_result(enabled=enabled, overlay=overlay)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [item for item in yaml.safe_load_all(result.stdout) if item]


def find(items: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return next(
        item
        for item in items
        if item.get("kind") == kind and item.get("metadata", {}).get("name") == name
    )


def main() -> None:
    predev = render(enabled=True)
    config = find(predev, "ConfigMap", "caritas-signoz-observability")
    job = find(predev, "Job", "caritas-signoz-observability")

    assert "signoz_observability.py" in config["data"]
    catalog = yaml.safe_load(config["data"]["observability-catalog.json"])
    assert len(catalog["dashboards"]) == 3
    assert len(catalog["alerts"]) == 6

    annotations = job["metadata"]["annotations"]
    assert "post-install" in annotations["helm.sh/hook"]
    assert "post-upgrade" in annotations["helm.sh/hook"]
    assert annotations["helm.sh/hook-delete-policy"] == "before-hook-creation"

    pod = job["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    container = pod["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["runAsUser"] == 65532
    assert container["securityContext"]["runAsGroup"] == 65532
    assert container["resources"]["requests"]["cpu"]
    assert container["resources"]["limits"]["memory"]

    env = {item["name"]: item for item in container["env"]}
    assert env["ORISO_ENVIRONMENT"]["value"] == "pre-dev"
    assert env["ORISO_CLUSTER_NAME"]["value"] == "oriso-predev"
    assert env["SIGNOZ_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "caritas-signoz-observability",
        "key": "apiKey",
    }
    assert env["SLACK_WEBHOOK_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "caritas-signoz-observability",
        "key": "slackWebhookUrl",
    }
    assert env["TEST_NOTIFICATION_ROUTE"]["value"] == "true"
    assert env["SIGNOZ_API_URL"]["value"] == "http://caritas-signoz:8080"
    assert "slack" not in str(container["args"]).lower()

    custom_port_result = render_result(
        enabled=True,
        extra_args=("--set", "signoz.signoz.service.port=18080"),
    )
    assert custom_port_result.returncode == 0, custom_port_result.stderr
    custom_port_documents = [
        item for item in yaml.safe_load_all(custom_port_result.stdout) if item
    ]
    custom_port_job = find(
        custom_port_documents, "Job", "caritas-signoz-observability"
    )
    custom_port_env = {
        item["name"]: item
        for item in custom_port_job["spec"]["template"]["spec"]["containers"][0][
            "env"
        ]
    }
    assert custom_port_env["SIGNOZ_API_URL"]["value"] == "http://caritas-signoz:18080"

    null_resources_result = render_result(
        enabled=True,
        extra_args=("--set-json", "signoz.orisoObservability.resources=null"),
    )
    assert null_resources_result.returncode == 0, null_resources_result.stderr
    null_resources_documents = [
        item for item in yaml.safe_load_all(null_resources_result.stdout) if item
    ]
    null_resources_job = find(
        null_resources_documents, "Job", "caritas-signoz-observability"
    )
    assert (
        null_resources_job["spec"]["template"]["spec"]["containers"][0][
            "resources"
        ]
        == {}
    )

    with tempfile.TemporaryDirectory() as temporary_directory:
        chart = pathlib.Path(temporary_directory)
        (chart / "templates").mkdir()
        (chart / "Chart.yaml").write_text(
            "apiVersion: v2\nname: missing-observability-assets\nversion: 0.1.0\n",
            encoding="utf-8",
        )
        template = pathlib.Path(
            CHART_DIR, "templates", "signoz", "observability-configmap.yaml"
        ).read_text(encoding="utf-8")
        (chart / "templates" / "observability-configmap.yaml").write_text(
            template, encoding="utf-8"
        )
        missing_assets = subprocess.run(
            [
                "helm",
                "template",
                "caritas",
                str(chart),
                "--set",
                "signoz.enabled=true",
                "--set",
                "signoz.orisoObservability.enabled=true",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert missing_assets.returncode != 0
        assert "scripts/signoz_observability.py must exist" in missing_assets.stderr

    dev = render(enabled=True, overlay="values-dev.yaml")
    dev_job = find(dev, "Job", "caritas-signoz-observability")
    dev_env = {
        item["name"]: item
        for item in dev_job["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert dev_env["ORISO_ENVIRONMENT"]["value"] == "dev"
    assert dev_env["ORISO_CLUSTER_NAME"]["value"] == "oriso-dev"

    disabled_names = {
        (item.get("kind"), item.get("metadata", {}).get("name"))
        for item in render(enabled=False)
    }
    assert ("ConfigMap", "caritas-signoz-observability") not in disabled_names
    assert ("Job", "caritas-signoz-observability") not in disabled_names

    without_backend = render_result(enabled=True, signoz_enabled=False)
    assert without_backend.returncode != 0
    assert (
        "signoz.orisoObservability.enabled=true requires signoz.enabled=true"
        in without_backend.stderr
    )
    without_infra = render_result(enabled=True, infra_enabled=False)
    assert without_infra.returncode != 0
    assert (
        "signoz.orisoObservability.enabled=true requires k8s-infra.enabled=true"
        in without_infra.stderr
    )

    print("PASS: managed SigNoz assets and fail-closed provisioning render correctly")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
