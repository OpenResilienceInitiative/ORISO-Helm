#!/usr/bin/env python3
"""Helm render contract for managed SigNoz observability resources."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from typing import Any
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


FIXTURE_DOMAIN = "render.oriso.internal"
API_KEY_SECRET = "signoz-observability-api-key"
BROWSER_READER_SECRET = "signoz-browser-vitals-reader"
BROWSER_CRON = "caritas-signoz-browser-vitals"


def render_result(
    *,
    enabled: bool | None,
    release: str = "caritas",
    environment: str = "pre-dev",
    cluster_name: str = "oriso-predev",
    signoz_enabled: bool = True,
    collector_enabled: bool = True,
    secret_names: bool = True,
    extra_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    command = [
        "helm",
        "template",
        release,
        CHART_DIR,
        "--namespace",
        "caritas",
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
        "userService.smtpUser=smtp-test-user",
        "--set-string",
        "userService.smtpPassword=smtp-test-password",
        "--set-string",
        f"signoz.signoz.env.signoz_global_external__url=https://{FIXTURE_DOMAIN}/signoz",
        "--set",
        f"signoz.enabled={'true' if signoz_enabled else 'false'}",
        "--set",
        f"signozCollector.enabled={'true' if collector_enabled else 'false'}",
        "--set-string",
        f"signozCollector.clusterName={cluster_name}",
        "--set-string",
        f"global.observability.deploymentEnvironment={environment}",
    ]
    if enabled is not None:
        command += ["--set", f"signoz.orisoObservability.enabled={str(enabled).lower()}"]
    if secret_names:
        command += [
            "--set-string",
            f"signoz.orisoObservability.apiKeySecret.name={API_KEY_SECRET}",
        ]
    command += list(extra_args)
    return subprocess.run(command, capture_output=True, text=True, check=False)


def render(**kwargs: Any) -> list[dict[str, Any]]:
    result = render_result(**kwargs)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [item for item in yaml.safe_load_all(result.stdout) if item]


def find(items: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return next(
        item
        for item in items
        if item.get("kind") == kind and item.get("metadata", {}).get("name") == name
    )


def check_observer_selector(documents: list[dict[str, Any]]) -> None:
    """The observer's real list selector must match every rendered Java workload."""
    path = os.path.join(CHART_DIR, "files", "signoz", "signoz_deployment_identity.py")
    spec = importlib.util.spec_from_file_location("signoz_deployment_identity", path)
    assert spec and spec.loader
    observer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observer)
    connection = Mock()
    connection.getresponse.return_value = Mock(status=200, read=Mock(return_value=b'{"items": []}'))
    observer._list_workloads(connection, "/api/v1/namespaces/caritas/pods", {})
    selector = parse_qs(urlsplit(connection.request.call_args.args[1]).query)["labelSelector"][0]
    key, _, values = selector.partition(" in ")
    selected = set(values.strip("()").split(","))
    for workload in observer.SERVICES:
        deployment = find(documents, "Deployment", workload)["spec"]
        assert deployment["selector"]["matchLabels"].get(key) in selected, workload
        assert deployment["template"]["metadata"]["labels"].get(key) in selected, workload
        names = [container["name"] for container in deployment["template"]["spec"]["containers"]]
        assert workload in names, (workload, names)


def check_deployment_identity(default_documents: list[dict[str, Any]]) -> None:
    name = "caritas-signoz-deployment-identity"
    assert all(item.get("metadata", {}).get("name") != name for item in default_documents)
    workloads = ("agencyservice", "consultingtypeservice", "tenantservice", "userservice")
    digest = "sha256:" + "1" * 64
    images = {
        workload: {"image": f"ghcr.io/example/{workload}@{digest}", "runtimeDigests": [digest]}
        for workload in workloads
    }
    flag = "signoz.orisoObservability.deploymentIdentity"
    args = ("--set", f"{flag}.enabled=true", "--set-json", f"{flag}.expectedImages={json.dumps(images)}")
    enabled = render(enabled=True, environment="dev", cluster_name="oriso-dev", extra_args=args)
    role = find(enabled, "Role", name)
    assert role["metadata"]["namespace"] == "caritas"
    assert role["rules"] == [
        {"apiGroups": ["apps"], "resources": ["deployments"],
         "resourceNames": list(workloads), "verbs": ["get"]},
        {"apiGroups": ["apps"], "resources": ["replicasets"], "verbs": ["list"]},
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]},
    ]
    assert find(enabled, "RoleBinding", name)["roleRef"]["kind"] == "Role"
    assert find(enabled, "ServiceAccount", name)["automountServiceAccountToken"] is False
    cron = find(enabled, "CronJob", name)
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"
    job = cron["spec"]["jobTemplate"]["spec"]
    assert job["activeDeadlineSeconds"] == 55 and job["backoffLimit"] == 0
    pod = job["template"]["spec"]
    assert pod["serviceAccountName"] == name
    assert pod["automountServiceAccountToken"] is True
    container = pod["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["ORISO_OTLP_HOST"] == "caritas-signoz-otel-collector:4318"
    assert env["ORISO_ENVIRONMENT"] == "dev"
    assert env["ORISO_CLUSTER_NAME"] == "oriso-dev"
    assert json.loads(env["ORISO_EXPECTED_IMAGES"]) == images
    assert "signoz_deployment_identity.py" in find(enabled, "ConfigMap", name)["data"]
    check_observer_selector(enabled)
    provision = find(enabled, "Job", "caritas-signoz-observability")
    assert {"name": "ORISO_DEPLOYMENT_IDENTITY_ENABLED", "value": "true"} in (
        provision["spec"]["template"]["spec"]["containers"][0]["env"]
    )

    assert render_result(enabled=False, extra_args=args).returncode != 0
    assert render_result(enabled=True, environment="prod", extra_args=args).returncode != 0
    assert render_result(enabled=True, extra_args=args[:2]).returncode != 0
    string_flag = ("--set-string", f"{flag}.enabled=false", *args[2:])
    result = render_result(enabled=True, environment="dev", cluster_name="oriso-dev", extra_args=string_flag)
    assert result.returncode != 0 and "enabled must be a boolean" in result.stderr, result.stderr


def browser_schedule_args(environment: str, secret: str = BROWSER_READER_SECRET) -> tuple[str, ...]:
    return (
        "--set", "global.observability.webVitals.enabled=true",
        "--set-string", f"signoz.otelCollector.additionalEnvs.ORISO_DEPLOYMENT_ENVIRONMENT={environment}",
        "--set", "signoz.orisoObservability.browserVitalsCheck.enabled=true",
        "--set-string", f"signoz.orisoObservability.browserVitalsCheck.apiKeySecretName={secret}",
    )


def check_browser_schedule() -> None:
    for environment in ("dev", "pre-dev"):
        docs = render(
            enabled=False, collector_enabled=False, environment=environment,
            extra_args=browser_schedule_args(environment) + (
                "--set", "signoz.signoz.service.port=18080",
            ),
        )
        schedule = find(docs, "CronJob", BROWSER_CRON)
        spec = schedule["spec"]
        assert spec["schedule"] == "*/5 * * * *"
        assert spec["concurrencyPolicy"] == "Forbid"
        assert 10 < spec["startingDeadlineSeconds"] < 300
        assert spec["successfulJobsHistoryLimit"] >= 2
        assert spec["failedJobsHistoryLimit"] >= 2
        assert spec["jobTemplate"]["metadata"]["labels"]["app.kubernetes.io/name"] == "signoz-browser-vitals"
        job = spec["jobTemplate"]["spec"]
        assert 0 < job["activeDeadlineSeconds"] < 300
        assert job["backoffLimit"] == 0
        pod = job["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False
        assert pod["restartPolicy"] == "Never"
        container, = pod["containers"]
        assert container["command"] == ["python3"]
        assert container["args"] == [
            "/opt/oriso/signoz/signoz_observability.py", "--mode", "check-browser-vitals",
        ]
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert container["securityContext"]["runAsNonRoot"] is True
        assert container["volumeMounts"] == [
            {"name": "assets", "mountPath": "/opt/oriso/signoz", "readOnly": True},
        ]
        assert pod["volumes"][0]["configMap"]["name"] == "caritas-signoz-observability"
        assert pod["volumes"][0]["configMap"]["items"] == [
            {"key": "signoz_observability.py", "path": "signoz_observability.py"},
        ]
        env = {item["name"]: item for item in container["env"]}
        assert set(env) == {"ORISO_ENVIRONMENT", "SIGNOZ_API_URL", "SIGNOZ_API_KEY"}
        assert env["ORISO_ENVIRONMENT"]["value"] == environment
        assert env["SIGNOZ_API_URL"]["value"] == "http://caritas-signoz:18080/signoz"
        assert env["SIGNOZ_API_KEY"]["valueFrom"]["secretKeyRef"] == {
            "name": BROWSER_READER_SECRET, "key": "apiKey",
        }
        assets = find(docs, "ConfigMap", "caritas-signoz-observability")
        assert set(assets["data"]) == {"signoz_observability.py"}
        assert not any(
            item["kind"] in ("Job", "Secret")
            and item["metadata"]["name"] in {"caritas-signoz-observability", BROWSER_READER_SECRET}
            for item in docs
        )

    both = render(enabled=True, extra_args=browser_schedule_args("pre-dev"))
    assert "observability-catalog.json" in find(both, "ConfigMap", "caritas-signoz-observability")["data"]
    find(both, "Job", "caritas-signoz-observability")
    find(both, "CronJob", BROWSER_CRON)

    long_name = render(
        enabled=False, release="reader-" + "a" * 44, extra_args=browser_schedule_args("pre-dev"),
    )
    scheduled, = [
        item for item in long_name if item["kind"] == "CronJob"
        and item["metadata"].get("labels", {}).get("app.kubernetes.io/name") == "signoz-browser-vitals"
    ]
    assert len(scheduled["metadata"]["name"]) <= 52

    enable = ("--set", "signoz.orisoObservability.browserVitalsCheck.enabled=true")
    for flags, error in (
        (enable, "requires global.observability.webVitals.enabled=true"),
        (("--set-string", "signoz.orisoObservability.browserVitalsCheck.enabled=false"), "enabled must be a boolean"),
        (browser_schedule_args("pre-dev", ""), "browserVitalsCheck.apiKeySecretName"),
        (browser_schedule_args("pre-dev", API_KEY_SECRET), "separate from the provisioning key"),
    ):
        result = render_result(enabled=False, extra_args=flags)
        assert result.returncode != 0, flags
        assert error in result.stderr, result.stderr


def main() -> None:
    predev = render(enabled=True)
    check_deployment_identity(predev)
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
        "name": API_KEY_SECRET,
        "key": "apiKey",
    }
    # The webhook stays in SigNoz's hand-made channel; the Job never sees it.
    assert "SLACK_WEBHOOK_URL" not in env
    assert env["SIGNOZ_CHANNEL_NAME"]["value"] == "oriso-alerts-slack"
    # Off by default: a hook run proves stored objects, not live telemetry,
    # and sends no Slack message (ORISO-Helm#295 review).
    assert env["ORISO_CONFORMANCE_CHECK"]["value"] == "false"
    assert "TEST_NOTIFICATION_ROUTE" not in env
    assert "value" not in env["SIGNOZ_API_KEY"]
    rendered_secrets = [item for item in predev if item.get("kind") == "Secret"]
    assert not any(
        item["metadata"]["name"] in {API_KEY_SECRET} for item in rendered_secrets
    ), "the chart must reference, never create, the observability Secrets"
    # SigNoz v0.135.1 strips the external-URL path from every API route, so
    # the in-cluster URL must carry /signoz or every call returns 404.
    assert env["SIGNOZ_API_URL"]["value"] == "http://caritas-signoz:8080/signoz"
    assert "slack" not in str(container["args"]).lower()

    custom_port_result = render_result(
        enabled=True,
        extra_args=("--set", "signoz.signoz.service.port=18080"),
    )
    assert custom_port_result.returncode == 0, custom_port_result.stderr
    custom_port_documents = [
        item for item in yaml.safe_load_all(custom_port_result.stdout) if item
    ]
    custom_port_job = find(custom_port_documents, "Job", "caritas-signoz-observability")
    custom_port_env = {
        item["name"]: item
        for item in custom_port_job["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert (
        custom_port_env["SIGNOZ_API_URL"]["value"]
        == "http://caritas-signoz:18080/signoz"
    )

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
        null_resources_job["spec"]["template"]["spec"]["containers"][0]["resources"]
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
        assert "files/signoz/signoz_observability.py must exist" in missing_assets.stderr

    dev = render(enabled=True, environment="dev", cluster_name="oriso-dev")
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
    assert ("CronJob", BROWSER_CRON) not in disabled_names

    without_backend = render_result(enabled=True, signoz_enabled=False)
    assert without_backend.returncode != 0
    assert (
        "signoz.orisoObservability.enabled=true requires signoz.enabled=true"
        in without_backend.stderr
    )
    without_collector = render_result(enabled=True, collector_enabled=False)
    assert without_collector.returncode != 0
    assert (
        "signoz.orisoObservability.enabled=true requires signozCollector.enabled=true"
        in without_collector.stderr
    )
    without_secrets = render_result(enabled=True, secret_names=False)
    assert without_secrets.returncode != 0
    assert "apiKeySecret.name must name an existing Secret" in without_secrets.stderr

    # Dev alone enables provisioning and must name the expected Secret.
    dev = yaml.safe_load(pathlib.Path(CHART_DIR, "values-dev.yaml").read_text(encoding="utf-8"))
    dev_managed = (dev.get("signoz") or {}).get("orisoObservability") or {}
    assert dev_managed.get("enabled") is True, "values-dev.yaml"
    assert (dev_managed.get("apiKeySecret") or {}).get("name") == API_KEY_SECRET, (
        "values-dev.yaml"
    )
    for overlay in (
        "values.yaml.default",
        "values-pre-dev.yaml",
        "values-prod.yaml",
    ):
        values = (
            yaml.safe_load(pathlib.Path(CHART_DIR, overlay).read_text(encoding="utf-8"))
            or {}
        )
        managed = ((values.get("signoz") or {}).get("orisoObservability")) or {}
        assert managed.get("enabled", False) is False, overlay
        assert (managed.get("browserVitalsCheck") or {}).get("enabled", False) is False, overlay

    layered_predev = render(
        enabled=None,
        extra_args=(
            "-f",
            os.path.join(CHART_DIR, "values-dev.yaml"),
            "-f",
            os.path.join(CHART_DIR, "values-pre-dev.yaml"),
            "--set-string",
            "signoz.signoz.env.signoz_global_external__url=https://predev.oriso.org/signoz",
        ),
    )
    assert not any(
        item.get("kind") == "Job"
        and item.get("metadata", {}).get("name") == "caritas-signoz-observability"
        for item in layered_predev
    ), "Pre-Dev layering must not enable managed observability"

    check_browser_schedule()
    print("PASS: managed SigNoz assets and fail-closed provisioning render correctly")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
