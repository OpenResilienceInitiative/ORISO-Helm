#!/usr/bin/env python3
"""Render contract for SigNoz on Dev: data protection, collector hardening,
trace identity and sampling, browser Web Vitals (ORISO SigNoz plan, session 1)."""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Any

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAVA_SERVICES = ("userservice", "agencyservice", "tenantservice", "consultingtypeservice")


def render(*overlays: str) -> list[dict[str, Any]]:
    command = ["helm", "template", "caritas", CHART_DIR, "--namespace", "caritas"]
    for values in (
        "values.yaml.default",
        "tests/fixtures/values-render-domain.yaml",
        *overlays,
        "secrets.yaml.default",
        "tests/fixtures/render-required-secrets.yaml",
    ):
        command.extend(["-f", os.path.join(CHART_DIR, values)])
    command.extend([
        "--set-string", "global.secrets.redisdefaultPass=test-redis-password",
        "--set-string", "userService.smtpUser=smtp-test-user",
        "--set-string", "userService.smtpPassword=smtp-test-password",
        "--set-string", "global.emailLegalOrganisationName=ORISO",
    ])
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def by_key(docs: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}


def env(workload: dict[str, Any]) -> dict[str, Any]:
    container = workload["spec"]["template"]["spec"]["containers"][0]
    return {item["name"]: item.get("value") for item in container.get("env", [])}


def assert_dev(docs: dict[tuple[str, str], dict[str, Any]]) -> None:
    # Data protection. The retain hook needs the bound volumes, which only a
    # cluster lookup sees; offline it must render nothing, never a ClusterRole
    # that could patch every volume in the cluster.
    for kind, name in docs:
        if "data-protection" in name:
            assert kind not in {"ClusterRole", "ClusterRoleBinding", "Job"}, (kind, name)
    backup_claim = docs[("PersistentVolumeClaim", "caritas-signoz-data-protection-backup")]
    assert backup_claim["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    cron = docs[("CronJob", "caritas-signoz-data-protection-backup")]
    pod = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    affinity = pod["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]
    assert affinity["topologyKey"] == "kubernetes.io/hostname"
    volumes = {v["name"]: v.get("persistentVolumeClaim", {}).get("claimName") for v in pod["volumes"]}
    assert volumes["signoz-db"] == "signoz-db-caritas-signoz-0"

    # Collector: memory_limiter first in every data pipeline, OTLP only. Browser
    # metrics have their own receiver and a pipeline that filters and relabels.
    collector_cm = docs[("ConfigMap", "caritas-signoz-otel-collector")]
    config = yaml.safe_load(next(iter(collector_cm["data"].values())))
    assert set(config["receivers"]) == {"otlp", "otlp/rum"}, config["receivers"]
    assert config["receivers"]["otlp/rum"]["protocols"]["http"]["max_request_body_size"] == 65536
    for name in ("traces", "metrics", "logs", "metrics/rum"):
        pipeline = config["service"]["pipelines"][name]
        assert pipeline["processors"][0] == "memory_limiter", (name, pipeline)
    for name in ("traces", "metrics", "logs"):
        assert config["service"]["pipelines"][name]["receivers"] == ["otlp"], name
    rum = config["service"]["pipelines"]["metrics/rum"]
    assert rum["receivers"] == ["otlp/rum"]
    assert rum["processors"].index("filter/rum") < rum["processors"].index("transform/rum")
    rum_filter = "\n".join(config["processors"]["filter/rum"]["metrics"]["metric"])
    assert "lcp|cls|inp|ttfb|fcp" in rum_filter and '"frontend"' in rum_filter
    resource = config["processors"]["transform/rum"]["metric_statements"][0]
    assert resource["context"] == "resource"
    assert resource["statements"][0] == 'keep_keys(attributes, ["service.name"])'
    datapoint = config["processors"]["transform/rum"]["metric_statements"][1]
    assert datapoint["context"] == "datapoint"
    assert datapoint["statements"][0].startswith('keep_keys(attributes, ["url.path_template"')
    collector_env = env(docs[("Deployment", "caritas-signoz-otel-collector")])
    assert collector_env["ORISO_DEPLOYMENT_ENVIRONMENT"] == "dev"
    collector = docs[("Deployment", "caritas-signoz-otel-collector")]
    limits = collector["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
    assert limits["memory"] == "1Gi"

    # Every trace kept on Dev, and each span says where and which build it came from.
    for name in JAVA_SERVICES:
        variables = env(docs[("Deployment", name)])
        attributes = variables["OTEL_RESOURCE_ATTRIBUTES"]
        assert attributes.startswith("deployment.environment=dev,service.version="), (name, attributes)
        assert not attributes.endswith("service.version="), (name, attributes)
        assert variables["MANAGEMENT_TRACING_SAMPLING_PROBABILITY"] == "1.0", name

    # Logs: the node agent runs and names a service for every pod.
    assert ("DaemonSet", "caritas-k8s-infra-otel-agent") in docs
    agent = yaml.safe_load(next(iter(docs[("ConfigMap", "caritas-k8s-infra-otel-agent")]["data"].values())))
    statements = "\n".join(agent["processors"]["transform/oriso_log_privacy"]["log_statements"])
    assert 'resource.attributes["k8s.deployment.name"]' in statements

    # Browser Web Vitals: same-origin ingest, bounded, wired into both SPAs.
    ingress = docs[("Ingress", "caritas-signoz-rum-ingress")]
    path = ingress["spec"]["rules"][0]["http"]["paths"][0]
    assert (path["path"], path["pathType"]) == ("/signoz-rum/v1/metrics", "Exact")
    assert path["backend"]["service"] == {"name": "caritas-signoz-otel-collector", "port": {"number": 4319}}
    annotations = ingress["metadata"]["annotations"]
    # rewrite-target would turn every path of the host into a regex location.
    assert "nginx.ingress.kubernetes.io/rewrite-target" not in annotations
    assert "rewrite ^/signoz-rum/v1/metrics$ /v1/metrics break;" in annotations["nginx.ingress.kubernetes.io/configuration-snippet"]
    assert annotations["nginx.ingress.kubernetes.io/proxy-body-size"] == "32k"
    assert "nginx.ingress.kubernetes.io/limit-rps" in annotations
    assert "$http_content_encoding" in annotations["nginx.ingress.kubernetes.io/configuration-snippet"]
    url = "https://dev.oriso.org/signoz-rum/v1/metrics"
    frontend = docs[("ConfigMap", "frontend-configmap")]["data"]
    admin = docs[("ConfigMap", "admin-configmap")]["data"]
    assert frontend["REACT_APP_OTEL_METRICS_URL"] == url and frontend["REACT_APP_OBSERVABILITY_ENABLED"] == "true"
    assert admin["VITE_OTEL_METRICS_URL"] == url and admin["VITE_OBSERVABILITY_ENABLED"] == "true"
    for name in ("frontend", "admin"):
        annotations = docs[("Deployment", name)]["spec"]["template"]["metadata"]["annotations"]
        assert "checksum/config" in annotations, name


def assert_default_off(docs: dict[tuple[str, str], dict[str, Any]]) -> None:
    names = {name for _, name in docs}
    assert not any("data-protection" in name or "rum-ingress" in name for name in names), names
    for name in JAVA_SERVICES:
        variables = env(docs[("Deployment", name)])
        assert "OTEL_RESOURCE_ATTRIBUTES" not in variables, name
        assert "MANAGEMENT_TRACING_SAMPLING_PROBABILITY" not in variables, name
    frontend = docs[("ConfigMap", "frontend-configmap")]["data"]
    assert frontend["REACT_APP_OTEL_METRICS_URL"] == "" and frontend["REACT_APP_OBSERVABILITY_ENABLED"] == "false"


def render_dev_with(overlay: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(overlay, handle)
    try:
        return by_key(render("values-dev.yaml", handle.name))
    finally:
        os.unlink(handle.name)


def backup_source(docs: dict[tuple[str, str], dict[str, Any]]) -> str | None:
    cron = docs.get(("CronJob", "caritas-signoz-data-protection-backup"))
    if cron is None:
        return None
    volumes = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["volumes"]
    return {v["name"]: v.get("persistentVolumeClaim", {}).get("claimName") for v in volumes}["signoz-db"]


def assert_review_findings() -> None:
    # A numeric 0.0 must disable sampling, not fall back to Spring's default.
    docs = render_dev_with({"global": {"observability": {"tracingSamplingProbability": 0.0}}})
    for service in JAVA_SERVICES:
        assert env(docs[("Deployment", service)])["MANAGEMENT_TRACING_SAMPLING_PROBABILITY"] == "0", service

    # E2E runs are selectable by their baggage field on Dev only.
    dev = by_key(render("values-dev.yaml"))
    for service in JAVA_SERVICES:
        values = env(dev[("Deployment", service)])
        assert values["MANAGEMENT_TRACING_BAGGAGE_REMOTEFIELDS"] == "test.run.id", service
        assert values["MANAGEMENT_TRACING_BAGGAGE_TAGFIELDS"] == "test.run.id", service
    docs = render_dev_with({"global": {"observability": {"testRunBaggage": False}}})
    assert "MANAGEMENT_TRACING_BAGGAGE_TAGFIELDS" not in env(docs[("Deployment", "userservice")])

    # The backup reads the claim the SigNoz StatefulSet really mounts.
    docs = render_dev_with({"signoz": {"signoz": {"persistence": {"existingClaim": "own-signoz-db"}}}})
    assert backup_source(docs) == "own-signoz-db"
    docs = render_dev_with({"signoz": {"signoz": {"persistence": {"enabled": False}}}})
    assert backup_source(docs) is None, "no SQLite claim, no backup"

    # Plain-text logs of Jobs, CronJobs, DaemonSets and bare Pods get a service name too.
    agent = [doc for (kind, _), doc in render_dev_with({}).items()
             if kind == "ConfigMap" and "privacy policy" in str(doc.get("data", {}))]
    assert agent, "agent config not rendered"
    config = str(agent[0]["data"])
    for attribute in ("k8s.cronjob.name", "k8s.job.name", "k8s.daemonset.name", "k8s.pod.name"):
        assert f'set(resource.attributes["service.name"], resource.attributes["{attribute}"])' in config, attribute
    assert config.index("k8s.cronjob.name\"])") < config.index("k8s.job.name\"])"), "CronJob name before the generated Job name"


def render_dev_fails(overlay: dict[str, Any]) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(overlay, handle)
    try:
        render("values-dev.yaml", handle.name)
    except AssertionError as error:
        return str(error)
    finally:
        os.unlink(handle.name)
    raise AssertionError(f"render succeeded with {overlay}")


def retain_claims(docs: dict[tuple[str, str], dict[str, Any]]) -> list[str]:
    config = docs[("ConfigMap", "caritas-signoz-data-protection")]
    return config["metadata"]["annotations"]["oriso.org/retain-claims"].split(",")


def assert_second_review_findings() -> None:
    # The retain hook protects the claims the vendored charts really mount.
    assert retain_claims(render_dev_with({})) == [
        "signoz-db-caritas-signoz-0",
        "data-volumeclaim-template-chi-caritas-clickhouse-cluster-0-0-0",
        "data-caritas-zookeeper-0",
    ]
    docs = render_dev_with({"signoz": {"clickhouse": {
        "persistence": {"existingClaim": "own-clickhouse"},
        "zookeeper": {"persistence": {"existingClaim": "own-zookeeper"}},
    }}})
    assert retain_claims(docs)[1:] == ["own-clickhouse", "own-zookeeper"]
    docs = render_dev_with({"signoz": {"clickhouse": {"persistence": {"enabled": False}}}})
    assert not any("clickhouse" in claim for claim in retain_claims(docs))

    # The backup lands next to the SigNoz pod even with a renamed component.
    docs = render_dev_with({"signoz": {"signoz": {"name": "observer"}}})
    pod = docs[("CronJob", "caritas-signoz-data-protection-backup")]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    selector = pod["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]["labelSelector"]
    assert selector["matchLabels"]["app.kubernetes.io/component"] == "observer"

    # Without TLS the browsers get an http ingest URL.
    docs = render_dev_with({"global": {"enableTls": False}})
    assert docs[("ConfigMap", "frontend-configmap")]["data"]["REACT_APP_OTEL_METRICS_URL"] == (
        "http://dev.oriso.org/signoz-rum/v1/metrics")

    # The DB monitoring hook fails at render time without images, and names
    # the store that never became ready.
    error = render_dev_fails({"online-counseling-mariadb": {"imageName": ""}})
    assert "online-counseling-mariadb.imageName is required for dbMetrics" in error, error
    job = next(doc for (kind, name), doc in render_dev_with({}).items()
               if kind == "Job" and "db-monitoring-reconcile" in name)
    script = str(job["spec"]["template"]["spec"])
    for store in ("MariaDB", "MongoDB", "RabbitMQ"):
        assert f"{store} not ready after 5 minutes" in script, store

    # The public browser receiver keeps only values Frontend and Admin send.
    config = yaml.safe_load(next(iter(docs[("ConfigMap", "caritas-signoz-otel-collector")]["data"].values())))
    statements = config["processors"]["transform/rum"]["metric_statements"][1]["statements"]
    assert any(line.startswith('replace_pattern(attributes["url.path_template"]') for line in statements)
    for key in ("url.path_template", "outcome", "transport", "stage", "cause", "category"):
        assert any(line.startswith(f'set(attributes["{key}"], "other") where') for line in statements), key


def main() -> None:
    assert_dev(by_key(render("values-dev.yaml")))
    assert_default_off(by_key(render()))
    assert_review_findings()
    assert_second_review_findings()
    print("PASS: SigNoz on Dev keeps its data, bounds the collector, and labels every signal")


if __name__ == "__main__":
    main()
