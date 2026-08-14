#!/usr/bin/env python3
"""Render contract for privacy-safe SigNoz Kubernetes infrastructure signals."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

import yaml


CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def render(
    *,
    signoz_enabled: bool,
    infra_enabled: bool,
    environment: str = "pre-dev",
    cluster_name: str = "oriso-predev",
    overlay: str | None = None,
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
    ]
    if overlay:
        command.extend(["-f", os.path.join(CHART_DIR, overlay)])
    command.extend(
        [
            "--set-string",
            "global.secrets.redisdefaultPass=test-redis-password",
            "--set-string",
            "userService.smtpUser=smtp-test-user",
            "--set-string",
            "userService.smtpPassword=smtp-test-password",
            "--set-string",
            "signoz.signoz.env.signoz_global_external__url=https://your-domain.example.com/signoz",
            "--set",
            f"signoz.enabled={'true' if signoz_enabled else 'false'}",
            "--set",
            f"k8s-infra.enabled={'true' if infra_enabled else 'false'}",
            "--set-string",
            f"global.observability.deploymentEnvironment={environment}",
            "--set-string",
            f"k8s-infra.global.deploymentEnvironment={environment}",
            "--set-string",
            f"k8s-infra.global.clusterName={cluster_name}",
        ]
    )
    return subprocess.run(command, capture_output=True, text=True, check=False)


def documents(result: subprocess.CompletedProcess[str]) -> list[dict[str, Any]]:
    if result.returncode:
        raise AssertionError(result.stderr)
    return [item for item in yaml.safe_load_all(result.stdout) if item]


def find(items: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return next(
        item
        for item in items
        if item.get("kind") == kind and item.get("metadata", {}).get("name") == name
    )


def container(workload: dict[str, Any]) -> dict[str, Any]:
    return workload["spec"]["template"]["spec"]["containers"][0]


def env_by_name(workload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in container(workload)["env"]}


def assert_resources(workload: dict[str, Any]) -> None:
    resources = container(workload)["resources"]
    for boundary in ("requests", "limits"):
        assert resources[boundary]["cpu"]
        assert resources[boundary]["memory"]


def parse_config(config_map: dict[str, Any], key: str) -> dict[str, Any]:
    return yaml.safe_load(config_map["data"][key])


def main() -> None:
    enabled = documents(render(signoz_enabled=True, infra_enabled=True))
    agent = find(enabled, "DaemonSet", "caritas-k8s-infra-otel-agent")
    cluster_collector = find(
        enabled,
        "Deployment",
        "caritas-k8s-infra-otel-deployment",
    )
    assert_resources(agent)
    assert_resources(cluster_collector)

    for workload in (agent, cluster_collector):
        env = env_by_name(workload)
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"]["value"] == (
            "caritas-signoz-otel-collector:4318"
        )
        assert env["K8S_CLUSTER_NAME"]["value"] == "oriso-predev"
        assert env["DEPLOYMENT_ENVIRONMENT"]["value"] == "pre-dev"
        security = container(workload)["securityContext"]
        assert security["allowPrivilegeEscalation"] is False
        assert security["readOnlyRootFilesystem"] is True
        assert security["capabilities"]["drop"] == ["ALL"]

    agent_ports = container(agent).get("ports", [])
    assert not {4317, 4318} & {port["containerPort"] for port in agent_ports}

    agent_config = parse_config(
        find(enabled, "ConfigMap", "caritas-k8s-infra-otel-agent"),
        "otel-agent-config.yaml",
    )
    assert {"filelog/k8s", "hostmetrics", "kubeletstats"} <= set(
        agent_config["receivers"]
    )
    filelog = agent_config["receivers"]["filelog/k8s"]
    assert filelog["include"] == [
        "/var/log/pods/${env:K8S_NAMESPACE}_*/*/*.log"
    ]
    assert filelog["start_at"] == "end"

    privacy = agent_config["processors"]["transform/oriso_log_privacy"]
    assert privacy["error_mode"] == "propagate"
    statements = "\n".join(privacy["log_statements"])
    assert "ParseJSON(log.body)" in statements
    assert "set(log.cache," in statements
    assert 'cache["request"]["correlationId"]' not in statements
    assert 'cache["log"]["message"]' not in statements
    assert 'cache["log"]["stack"]' not in statements
    assert "set(log.body," in statements
    assert "log.trace_id.string" in statements
    assert "log.span_id.string" in statements
    assert "log body suppressed" in statements
    assert "trace.id" in statements
    assert "span.id" in statements
    assert "log.level" in statements
    assert "log.logger" in statements
    for forbidden in ("log.message", "log.stack", "correlationId", "email", "token"):
        assert forbidden not in statements

    log_pipeline = agent_config["service"]["pipelines"]["logs"]
    assert log_pipeline["receivers"] == ["filelog/k8s"]
    assert "transform/oriso_log_privacy" in log_pipeline["processors"]
    assert log_pipeline["processors"].index("transform/oriso_log_privacy") < (
        log_pipeline["processors"].index("batch")
    )
    assert log_pipeline["exporters"] == ["otlphttp"]

    metrics_pipeline = agent_config["service"]["pipelines"]["metrics"]
    assert {"hostmetrics", "kubeletstats"} <= set(metrics_pipeline["receivers"])
    assert metrics_pipeline["exporters"] == ["otlphttp"]

    deployment_config = parse_config(
        find(enabled, "ConfigMap", "caritas-k8s-infra-otel-deployment"),
        "otel-deployment-config.yaml",
    )
    assert "k8s_cluster" in deployment_config["receivers"]
    assert "k8s_events" in deployment_config["receivers"]
    assert deployment_config["service"]["pipelines"]["metrics/internal"][
        "exporters"
    ] == ["otlphttp"]
    assert deployment_config["service"]["pipelines"]["logs"]["exporters"] == [
        "otlphttp"
    ]
    for config in (agent_config, deployment_config):
        self_metrics = config["service"]["telemetry"]["metrics"]
        assert self_metrics["level"] == "detailed"
        exporter = self_metrics["readers"][0]["periodic"]["exporter"]["otlp"]
        assert exporter["endpoint"] == "${env:OTEL_EXPORTER_OTLP_ENDPOINT}"

    for component in ("otel-agent", "otel-deployment"):
        role = find(
            enabled,
            "ClusterRole",
            f"caritas-k8s-infra-{component}-caritas",
        )
        resources = {
            resource
            for rule in role["rules"]
            for resource in rule.get("resources", [])
        }
        verbs = {verb for rule in role["rules"] for verb in rule.get("verbs", [])}
        assert "secrets" not in resources
        assert "*" not in resources
        assert "*" not in verbs
        assert not {"create", "delete", "patch", "update"} & verbs

    disabled_names = {
        (item.get("kind"), item.get("metadata", {}).get("name"))
        for item in documents(render(signoz_enabled=False, infra_enabled=False))
    }
    assert not any("k8s-infra" in str(name) for _, name in disabled_names)

    invalid = render(signoz_enabled=False, infra_enabled=True)
    assert invalid.returncode != 0
    assert "k8s-infra.enabled=true requires signoz.enabled=true" in invalid.stderr

    predev = documents(
        render(
            signoz_enabled=True,
            infra_enabled=True,
            environment="pre-dev",
            cluster_name="oriso-predev",
            overlay="values-pre-dev.yaml",
        )
    )
    dev = documents(
        render(
            signoz_enabled=True,
            infra_enabled=True,
            environment="dev",
            cluster_name="oriso-dev",
            overlay="values-dev.yaml",
        )
    )
    assert env_by_name(find(predev, "DaemonSet", "caritas-k8s-infra-otel-agent"))[
        "K8S_CLUSTER_NAME"
    ]["value"] == "oriso-predev"
    assert env_by_name(find(dev, "DaemonSet", "caritas-k8s-infra-otel-agent"))[
        "K8S_CLUSTER_NAME"
    ]["value"] == "oriso-dev"

    print("PASS: official SigNoz k8s-infra signals are scoped, identified, and privacy-safe")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
