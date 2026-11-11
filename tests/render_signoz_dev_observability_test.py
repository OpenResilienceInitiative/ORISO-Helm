#!/usr/bin/env python3
"""Render contract for SigNoz on Dev: data protection, collector hardening,
trace identity and sampling, browser Web Vitals (ORISO SigNoz plan, session 1)."""

from __future__ import annotations

from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAVA_SERVICES = ("userservice", "agencyservice", "tenantservice", "consultingtypeservice")


def render(
    *overlays: str,
    release: str = "caritas",
    chart: str = CHART_DIR,
    helm_args: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    command = ["helm", "template", release, chart, "--namespace", "caritas", *helm_args]
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
    job = docs[("Job", "caritas-signoz-observability")]
    job_env = job["spec"]["template"]["spec"]["containers"][0]["env"]
    api_key = next(item for item in job_env if item["name"] == "SIGNOZ_API_KEY")
    assert api_key["valueFrom"]["secretKeyRef"] == {
        "name": "signoz-observability-api-key",
        "key": "apiKey",
    }
    assert "value" not in api_key

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
    assert set(config["receivers"]) == {"otlp", "otlp/rum", "otlp/e2e"}, config["receivers"]
    assert config["receivers"]["otlp/rum"]["protocols"]["http"]["max_request_body_size"] == 65536
    for name in ("traces", "metrics", "logs", "metrics/rum", "traces/e2e"):
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
    assert (
        'if ($http_content_encoding != "") { return 415; }'
        in annotations["nginx.ingress.kubernetes.io/configuration-snippet"]
    )
    url = "https://dev.oriso.org/signoz-rum/v1/metrics"
    frontend = docs[("ConfigMap", "frontend-configmap")]["data"]
    admin = docs[("ConfigMap", "admin-configmap")]["data"]
    assert frontend["REACT_APP_OTEL_METRICS_URL"] == url and frontend["REACT_APP_OBSERVABILITY_ENABLED"] == "true"
    assert admin["VITE_OTEL_METRICS_URL"] == url and admin["VITE_OBSERVABILITY_ENABLED"] == "true"
    for name in ("frontend", "admin"):
        annotations = docs[("Deployment", name)]["spec"]["template"]["metadata"]["annotations"]
        assert "checksum/config" in annotations, name


def assert_e2e_trace_ingest() -> None:
    """Authenticated span ingest for the Dev nightly E2E run (ORISO-E2E#97)."""
    docs = by_key(render("values-dev.yaml"))
    ingress = docs[("Ingress", "caritas-signoz-e2e-ingress")]
    rules = ingress["spec"]["rules"]
    assert len(rules) == 1 and len(rules[0]["http"]["paths"]) == 1, rules
    assert rules[0]["host"] == "dev.oriso.org"
    assert ingress["spec"]["tls"][0]["hosts"] == ["dev.oriso.org"]
    path = rules[0]["http"]["paths"][0]
    assert (path["path"], path["pathType"]) == ("/signoz-e2e/v1/traces", "Exact")
    assert path["backend"]["service"] == {"name": "caritas-signoz-otel-collector", "port": {"number": 4321}}
    annotations = ingress["metadata"]["annotations"]
    # The token is checked by ingress-nginx itself against a Secret named in values.
    assert annotations["nginx.ingress.kubernetes.io/auth-type"] == "basic"
    auth_secret = annotations["nginx.ingress.kubernetes.io/auth-secret"]
    assert auth_secret == "signoz-e2e-ingest-auth"
    assert annotations["nginx.ingress.kubernetes.io/auth-secret-type"] == "auth-file"
    rendered_ingress = str(ingress)
    assert "password" not in rendered_ingress.lower()
    assert not any(prefix in rendered_ingress for prefix in ("$2a$", "$2b$", "$2y$"))
    assert ("Secret", auth_secret) not in docs, "chart must only reference the E2E authentication Secret"
    snippet = annotations["nginx.ingress.kubernetes.io/configuration-snippet"]
    assert "if ($request_method != POST) { return 405; }" in snippet
    assert 'if ($http_content_encoding != "") { return 415; }' in snippet
    rewrite = "rewrite ^/signoz-e2e/v1/traces$ /v1/traces break;"
    exact_uri = 'if ($request_uri != "/signoz-e2e/v1/traces") { return 404; }'
    assert exact_uri in snippet and snippet.index(exact_uri) < snippet.index(rewrite), snippet
    assert "nginx.ingress.kubernetes.io/rewrite-target" not in annotations
    assert annotations["nginx.ingress.kubernetes.io/proxy-body-size"] == "256k"
    assert "nginx.ingress.kubernetes.io/limit-rps" in annotations
    # Dedicated receiver/pipeline: own port, body cap, filter, fixed environment, source tag.
    collector_cm = docs[("ConfigMap", "caritas-signoz-otel-collector")]
    config = yaml.safe_load(next(iter(collector_cm["data"].values())))
    e2e_http = config["receivers"]["otlp/e2e"]["protocols"]["http"]
    assert e2e_http["endpoint"] == "0.0.0.0:4321" and e2e_http["max_request_body_size"] == 262144, e2e_http
    assert config["service"]["pipelines"]["traces"]["receivers"] == ["otlp"], "main pipeline must not take the E2E receiver"
    pipeline = config["service"]["pipelines"]["traces/e2e"]
    assert pipeline["receivers"] == ["otlp/e2e"], pipeline
    procs = pipeline["processors"]
    assert procs[0] == "memory_limiter" and procs.index("filter/e2e") < procs.index("transform/e2e"), procs
    drop_rule = config["processors"]["filter/e2e"]["traces"]["span"]
    assert drop_rule == ['resource.attributes["service.name"] != "oriso-e2e"'], drop_rule
    statements = config["processors"]["transform/e2e"]["trace_statements"][0]
    assert statements["context"] == "resource"
    assert 'set(attributes["deployment.environment"], "${env:ORISO_DEPLOYMENT_ENVIRONMENT}")' in statements["statements"]
    assert 'set(attributes["oriso.signal.source"], "e2e")' in statements["statements"]
    assert env(docs[("Deployment", "caritas-signoz-otel-collector")])["ORISO_DEPLOYMENT_ENVIRONMENT"] == "dev"
    service = docs[("Service", "caritas-signoz-otel-collector")]["spec"]
    assert service["type"] == "ClusterIP"
    service_ports = {port["name"]: port for port in service["ports"]}
    assert service_ports["otlp-e2e"]["port"] == 4321 and service_ports["otlp-http"]["port"] == 4318, service_ports
    assert service_ports["otlp-e2e"].get("protocol", "TCP") == "TCP"
    assert path["backend"]["service"]["port"]["number"] == service_ports["otlp-e2e"]["port"]
    collector = docs[("Deployment", "caritas-signoz-otel-collector")]["spec"]["template"]["spec"]["containers"][0]
    port = next(port for port in collector["ports"] if port["name"] == "otlp-e2e")
    assert port["containerPort"] == 4321 and port.get("protocol", "TCP") == "TCP", port
    error = render_dev_fails({"signoz": {"otelCollector": {"ports": {"otlp-e2e": {"enabled": False}}}}})
    assert "e2eTraceIngest.enabled=true requires signoz.otelCollector.ports.otlp-e2e.enabled=true" in error, error
    error = render_dev_fails({"signoz": {"otelCollector": {"ports": {"otlp-e2e": {"containerPort": 4318}}}}})
    assert "otlp-e2e.containerPort must be 4321" in error, error
    error = render_dev_fails({"signoz": {"otelCollector": {"ports": {"otlp-e2e": {"servicePort": 4318}}}}})
    assert "otlp-e2e.servicePort conflicts" in error, error
    error = render_dev_fails({"signoz": {"otelCollector": {"additionalEnvs": {"ORISO_DEPLOYMENT_ENVIRONMENT": "prod"}}}})
    assert "ORISO_DEPLOYMENT_ENVIRONMENT must equal" in error, error
    error = render_dev_fails({"global": {"observability": {"e2eTraceIngest": {"authSecret": ""}}}})
    assert "e2eTraceIngest.authSecret must name an existing basic-auth Secret" in error, error
    # Default off, and off without SigNoz.
    assert ("Ingress", "caritas-signoz-e2e-ingress") not in by_key(render())


def assert_e2e_ingest_disabled(docs: dict[tuple[str, str], dict[str, Any]]) -> None:
    assert ("Ingress", "caritas-signoz-e2e-ingress") not in docs
    collector = "caritas-signoz-otel-collector"
    service_ports = docs[("Service", collector)]["spec"]["ports"]
    assert all(port["name"] != "otlp-e2e" for port in service_ports)
    pod = docs[("Deployment", collector)]["spec"]["template"]["spec"]
    assert all(port["name"] != "otlp-e2e" for port in pod["containers"][0]["ports"])
    config = yaml.safe_load(docs[("ConfigMap", collector)]["data"]["otel-collector-config.yaml"])
    assert "otlp/e2e" not in config["receivers"]
    assert not {"filter/e2e", "transform/e2e"} & config["processors"].keys()
    pipelines = config["service"]["pipelines"]
    assert "traces/e2e" not in pipelines
    assert {"traces", "metrics", "logs", "metrics/rum"} <= pipelines.keys()


def assert_e2e_trace_ingest_scope() -> None:
    assert_e2e_ingest_disabled(by_key(render("values-dev.yaml", "values-pre-dev.yaml")))
    assert_e2e_ingest_disabled(render_dev_with({
        "global": {"observability": {"e2eTraceIngest": {"enabled": False}}},
        "signoz": {"otelCollector": {"ports": {"otlp-e2e": {"enabled": False}}}},
    }))
    invalid = [
        ({"global": {"observability": {"e2eTraceIngest": {"enabled": False}}}},
         "otlp-e2e.enabled=true requires global.observability.e2eTraceIngest.enabled=true"),
        ({
            "global": {"observability": {"deploymentEnvironment": "pre-dev"}},
            "signoz": {"otelCollector": {"additionalEnvs": {"ORISO_DEPLOYMENT_ENVIRONMENT": "pre-dev"}}},
        }, "E2E trace ingest requires the Dev environment and dev.oriso.org"),
        ({
            "global": {"domainName": "alternate.render.oriso.internal"},
            "signoz": {"signoz": {"env": {
                "signoz_global_external__url": "https://alternate.render.oriso.internal/signoz",
            }}},
        }, "E2E trace ingest requires the Dev environment and dev.oriso.org"),
        ({"signoz": {"otelCollector": {"service": {"type": "NodePort"}}}},
         "E2E trace ingest requires a ClusterIP collector Service"),
        ({"signoz": {"otelCollector": {"service": {"type": "LoadBalancer"}}}},
         "E2E trace ingest requires a ClusterIP collector Service"),
        ({"signoz": {"otelCollector": {"ingress": {"enabled": True}}}},
         "E2E trace ingest requires a ClusterIP collector Service"),
        ({"signoz": {"otelCollector": {"ports": {"otlp-e2e": {"protocol": "UDP"}}}}},
         "otlp-e2e.protocol must be TCP"),
        ({
            "global": {"observability": {
                "otlpEnabled": False, "autoEnableWithSignoz": False, "appMetrics": False,
                "dbMetrics": False, "webVitals": {"enabled": False},
            }},
            "signozCollector": {"enabled": False},
            "signoz": {"orisoObservability": {"enabled": False}, "otelCollector": {"replicaCount": 0}},
        }, "E2E trace ingest requires a running bundled SigNoz collector"),
    ]
    for overrides, message in invalid:
        error = render_dev_fails(overrides)
        assert message in error, error


def assert_default_off(docs: dict[tuple[str, str], dict[str, Any]]) -> None:
    names = {name for _, name in docs}
    assert not any("data-protection" in name or "rum-ingress" in name or "e2e-ingress" in name for name in names), names
    for name in JAVA_SERVICES:
        variables = env(docs[("Deployment", name)])
        assert "OTEL_RESOURCE_ATTRIBUTES" not in variables, name
        assert "MANAGEMENT_TRACING_SAMPLING_PROBABILITY" not in variables, name
    frontend = docs[("ConfigMap", "frontend-configmap")]["data"]
    assert frontend["REACT_APP_OTEL_METRICS_URL"] == "" and frontend["REACT_APP_OBSERVABILITY_ENABLED"] == "false"


def render_dev_with(
    overlay: dict[str, Any], *helm_args: str, chart: str = CHART_DIR,
) -> dict[tuple[str, str], dict[str, Any]]:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(overlay, handle)
    try:
        return by_key(render("values-dev.yaml", handle.name, chart=chart, helm_args=helm_args))
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


def render_dev_fails(overlay: dict[str, Any], *helm_args: str, chart: str = CHART_DIR) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(overlay, handle)
    try:
        render("values-dev.yaml", handle.name, chart=chart, helm_args=helm_args)
    except AssertionError as error:
        return str(error)
    finally:
        os.unlink(handle.name)
    raise AssertionError(f"render succeeded with {overlay}")


def retain_claims(docs: dict[tuple[str, str], dict[str, Any]]) -> list[str]:
    config = docs[("ConfigMap", "caritas-signoz-data-protection")]
    return config["metadata"]["annotations"]["oriso.org/retain-claims"].split(",")


def assert_second_review_findings() -> None:
    # The retain hook protects the claims the vendored charts really mount:
    # every shard, replica and ZooKeeper member, in each chart's namespace.
    assert retain_claims(render_dev_with({})) == [
        "caritas/signoz-db-caritas-signoz-0",
        "caritas/data-volumeclaim-template-chi-caritas-clickhouse-cluster-0-0-0",
        "caritas/data-caritas-zookeeper-0",
    ]
    docs = render_dev_with({"signoz": {"clickhouse": {
        "persistence": {"existingClaim": "own-clickhouse"},
        "zookeeper": {"persistence": {"existingClaim": "own-zookeeper"}},
    }}})
    assert retain_claims(docs)[1:] == ["caritas/own-clickhouse", "caritas/own-zookeeper"]
    docs = render_dev_with({"signoz": {"clickhouse": {"persistence": {"enabled": False}}}})
    assert not any("clickhouse" in claim for claim in retain_claims(docs))
    docs = render_dev_with({"signoz": {"clickhouse": {
        "namespace": "telemetry",
        "layout": {"shardsCount": 2, "replicasCount": 2},
        "zookeeper": {"replicaCount": 3, "namespaceOverride": "coordination"},
    }}})
    chi = "data-volumeclaim-template-chi-caritas-clickhouse-cluster"
    assert retain_claims(docs)[1:] == [
        f"telemetry/{chi}-0-0-0", f"telemetry/{chi}-0-1-0", f"telemetry/{chi}-1-0-0", f"telemetry/{chi}-1-1-0",
        "coordination/data-caritas-zookeeper-0", "coordination/data-caritas-zookeeper-1",
        "coordination/data-caritas-zookeeper-2",
    ]
    docs = render_dev_with({"signoz": {"clickhouse": {"zookeeper": {"replicaCount": 2, "dataLogDir": "/bitnami/zookeeper/log"}}}})
    assert retain_claims(docs)[-2:] == ["caritas/data-log-caritas-zookeeper-0", "caritas/data-log-caritas-zookeeper-1"]
    docs = render_dev_with({"signoz": {"clickhouse": {"zookeeper": {"persistence": {"dataLogDir": {"existingClaim": "own-log"}}}}}})
    assert retain_claims(docs)[-1] == "caritas/own-log"
    docs = render_dev_with({"signoz": {"postgresql": {"enabled": True, "replicaCount": 2}}})
    claims = retain_claims(docs)
    assert claims[:2] == ["caritas/pgdata-caritas-postgresql-0", "caritas/pgdata-caritas-postgresql-1"], claims
    assert not any("signoz-db" in claim for claim in claims), "no SQLite claim with PostgreSQL"
    docs = render_dev_with({"signoz": {"postgresql": {"enabled": True, "persistence": {"existingClaim": "own-pg"}}}})
    assert retain_claims(docs)[0] == "caritas/own-pg"
    docs = render_dev_with({"signoz": {"orisoDataProtection": {"claims": ["kept-here", "other/kept-there"]}}})
    assert retain_claims(docs) == ["caritas/kept-here", "other/kept-there"]

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
    # Old vhost permissions of the monitoring user go before it is recreated.
    rabbit = next((c.get("args") or c["command"])[-1] for c in job["spec"]["template"]["spec"]["initContainers"]
                  if "oriso-monitor" in str(c))
    assert rabbit.index('-X DELETE "$API/users/oriso-monitor"') < rabbit.index("-X PUT"), "delete before recreate"
    # Queue metrics need a permission record per vhost; it must grant no AMQP right.
    assert rabbit.index('"$API/users/oriso-monitor"\n') < rabbit.index('"$API/permissions/$encoded/oriso-monitor"'), rabbit
    assert """-d '{"configure":"","write":"","read":""}'""" in rabbit, rabbit

    # The public browser receiver keeps only values Frontend and Admin send.
    config = yaml.safe_load(next(iter(docs[("ConfigMap", "caritas-signoz-otel-collector")]["data"].values())))
    statements = config["processors"]["transform/rum"]["metric_statements"][1]["statements"]
    assert any(line.startswith('replace_pattern(attributes["url.path_template"]') for line in statements)
    for key in ("url.path_template", "outcome", "transport", "stage", "cause", "category"):
        assert any(line.startswith(f'set(attributes["{key}"], "other") where') and "IsMatch" in line
                   for line in statements), key
        not_string = next(i for i, line in enumerate(statements) if f'not IsString(attributes["{key}"])' in line)
        assert not_string < next(i for i, line in enumerate(statements) if "IsMatch" in line), key


def assert_backup_is_atomic() -> None:
    docs = by_key(render("values-dev.yaml"))
    config = docs[("ConfigMap", "caritas-signoz-data-protection")]
    cron = docs[("CronJob", "caritas-signoz-data-protection-backup")]
    container = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    fields = {item["name"]: item["valueFrom"]["fieldRef"]["fieldPath"]
              for item in container["env"] if "valueFrom" in item}
    assert fields == {"JOB_UID": "metadata.labels['batch.kubernetes.io/controller-uid']",
                      "POD_UID": "metadata.uid"}, fields
    assert container["terminationMessagePolicy"] == "File"
    job_uid = "75a198df-bb28-4e9e-9b0b-9e038b5d713f"
    pod_uid = "cf829cd6-8339-4b58-a912-81a3ef109a57"
    with tempfile.TemporaryDirectory() as root:
        signoz, backup, recovery, scripts = (
            Path(root) / name for name in ("signoz", "backup", "recovery", "scripts")
        )
        for directory in (signoz, backup, recovery, scripts):
            directory.mkdir()
        receipt_path = Path(root) / "termination-log"
        database = signoz / "signoz.db"
        (scripts / "backup.py").write_text(
            config["data"]["backup.py"]
            .replace('"/signoz/signoz.db"', f'"{database}"')
            .replace('"/backup"', f'"{backup}"')
            .replace('"/dev/termination-log"', f'"{receipt_path}"'),
            encoding="utf-8",
        )
        (scripts / "restore.py").write_text(config["data"]["restore.py"], encoding="utf-8")

        def run_backup(uid: str, keep: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, str(scripts / "backup.py")],
                env={**os.environ, "KEEP": keep, "JOB_UID": job_uid, "POD_UID": uid},
                capture_output=True, text=True, check=False, timeout=15,
            )

        with closing(sqlite3.connect(database)) as writer:
            assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.executescript("""
                CREATE TABLE organizations (id TEXT PRIMARY KEY);
                CREATE TABLE users (id TEXT PRIMARY KEY);
                CREATE TABLE dashboard (id TEXT PRIMARY KEY);
                CREATE TABLE rule (id TEXT PRIMARY KEY);
                INSERT INTO users VALUES ('synthetic-admin');
                INSERT INTO rule VALUES ('synthetic-rule');
            """)
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            writer.execute("INSERT INTO dashboard VALUES ('wal-only-dashboard')")
            writer.commit()
            # The current row is only in the active WAL, so copying signoz.db alone would lose it.
            with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro&immutable=1", uri=True)) as raw:
                assert raw.execute("SELECT id FROM dashboard").fetchall() == []
            legacy_partial = backup / ".signoz-20261017T000000Z.db.gz.part"
            legacy_partial.write_bytes(b"truncated")
            result = run_backup(pod_uid, "14")
            assert result.returncode == 0, result.stderr
            assert writer.execute("SELECT id FROM dashboard").fetchall() == [("wal-only-dashboard",)]

        (archive,) = backup.glob("signoz-*.db.gz")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        assert receipt == {"version": 1, "archive": archive.name, "jobUid": job_uid,
                           "podUid": pod_uid, "sha256": checksum, "bytes": archive.stat().st_size}, receipt
        assert archive.name.endswith(f"-{job_uid}-{pod_uid}-{checksum}.db.gz")
        assert archive.stat().st_mode & 0o777 == 0o600
        assert legacy_partial.read_bytes() == b"truncated"
        assert not (backup / ".signoz_metadata_stage").exists()
        result = subprocess.run(
            [sys.executable, str(scripts / "restore.py"), str(archive), str(recovery),
             "--sha256", receipt["sha256"], "--compressed-bytes", str(receipt["bytes"])],
            capture_output=True, text=True, check=False, timeout=15,
        )
        assert result.returncode == 0, result.stderr
        restored = recovery / "signoz.db"
        with closing(sqlite3.connect(f"{restored.as_uri()}?mode=ro&immutable=1", uri=True)) as readback:
            assert readback.execute("SELECT id FROM dashboard").fetchall() == [("wal-only-dashboard",)]
            assert readback.execute("SELECT id FROM users").fetchall() == [("synthetic-admin",)]
            assert readback.execute("SELECT id FROM rule").fetchall() == [("synthetic-rule",)]
        assert sorted(path.name for path in recovery.iterdir()) == ["signoz.db"]

        receipt_path.unlink()
        receipt_path.mkdir()
        result = run_backup("cf829cd6-8339-4b58-a912-81a3ef109a58", "1")
        assert result.returncode == 1, result.stdout
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == checksum
        assert len(list(backup.glob("signoz-*.db.gz"))) == 2

        receipt_path.rmdir()
        next_pod = "cf829cd6-8339-4b58-a912-81a3ef109a59"
        with (backup / ".signoz_backup.lock").open("r+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = run_backup(next_pod, "1")
            assert result.returncode == 1 and "Another metadata backup" in result.stderr, result.stderr
            assert not receipt_path.exists()

        result = run_backup(next_pod, "1")
        assert result.returncode == 0, result.stderr
        (latest,) = backup.glob("signoz-*.db.gz")
        assert json.loads(receipt_path.read_text(encoding="utf-8"))["archive"] == latest.name
        assert latest != archive and legacy_partial.is_file()


def assert_long_release_name() -> None:
    # A 23+ character release name must not push the CronJob past 52 characters.
    docs = render("values-dev.yaml", release="observability-staging-eu1")
    cron = next(doc for doc in docs if doc["kind"] == "CronJob" and doc["metadata"]["name"].startswith("observability-staging-eu1-signoz-data"))
    name = cron["metadata"]["name"]
    assert len(name) <= 52 and name.endswith("-backup"), name
    labels = cron["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
    assert labels["app"] == name, labels


def statefulset_claims(docs: dict[tuple[str, str], dict[str, Any]], name: str) -> list[str]:
    # The claims a rendered StatefulSet mounts: its own volumes and its templates' claims for member 0.
    workload = docs[("StatefulSet", name)]
    pod = workload["spec"]["template"]["spec"]
    mounted = [v["persistentVolumeClaim"]["claimName"] for v in pod.get("volumes", []) if "persistentVolumeClaim" in v]
    templated = [f"{t['metadata']['name']}-{name}-0" for t in workload["spec"].get("volumeClaimTemplates", [])]
    return mounted + templated


def assert_name_override_findings() -> None:
    # The retain hook derives every claim like the vendored StatefulSets, overrides and tpl included.
    docs = render_dev_with({"signoz": {"fullnameOverride": "telemetry", "clickhouse": {"zookeeper": {
        "fullnameOverride": "coord", "dataLogDir": "/bitnami/zookeeper/log"}}}})
    claims = retain_claims(docs)
    assert "caritas/signoz-db-telemetry-0" in claims, claims
    assert statefulset_claims(docs, "telemetry") == ["signoz-db-telemetry-0"]
    assert {"caritas/data-coord-0", "caritas/data-log-coord-0"} <= set(claims), claims
    assert set(statefulset_claims(docs, "coord")) == {"data-coord-0", "data-log-coord-0"}
    docs = render_dev_with({"signoz": {"nameOverride": "obs", "clickhouse": {"zookeeper": {"nameOverride": "zk"}}}})
    claims = retain_claims(docs)
    assert {"caritas/signoz-db-caritas-obs-0", "caritas/data-caritas-zk-0"} <= set(claims), claims
    assert statefulset_claims(docs, "caritas-zk") == ["data-caritas-zk-0"]
    docs = render_dev_with({"signoz": {
        "postgresql": {"enabled": True, "persistence": {"existingClaim": "{{ .Release.Name }}-pgdata"}},
        "clickhouse": {"zookeeper": {"persistence": {
            "existingClaim": "{{ .Release.Name }}-zk", "dataLogDir": {"existingClaim": "{{ .Release.Name }}-zk-log"}}}},
    }})
    claims = retain_claims(docs)
    assert {"caritas/caritas-pgdata", "caritas/caritas-zk", "caritas/caritas-zk-log"} <= set(claims), claims
    assert not any("{{" in claim for claim in claims), claims
    assert set(statefulset_claims(docs, "caritas-zookeeper")) == {"caritas-zk", "caritas-zk-log"}
    assert statefulset_claims(docs, "caritas-postgresql") == ["caritas-pgdata"]


def render_with_cluster_state(*overlays: str, release: str, replace: dict[str, str]) -> list[dict[str, Any]]:
    # Offline, lookup sees an empty cluster; a chart copy replaces a lookup line with the given state.
    template = "templates/signoz/signoz-data-protection.yaml"
    with tempfile.TemporaryDirectory() as tmp:
        chart = os.path.join(tmp, "chart")
        shutil.copytree(CHART_DIR, chart, ignore=shutil.ignore_patterns(".git"))
        path = os.path.join(chart, template)
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        for marker, state in replace.items():
            assert source.count(marker) == 1, marker
            source = source.replace(marker, state)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source)
        return render(*overlays, release=release, chart=chart)


def render_with_pending_volume(*overlays: str, release: str) -> list[dict[str, Any]]:
    return render_with_cluster_state(*overlays, release=release, replace={
        "{{- $pending := list -}}": '{{- $pending := list "pv-test" -}}'})


def backup_claim_class(docs: dict[tuple[str, str], dict[str, Any]]) -> str | None:
    return docs[("PersistentVolumeClaim", "caritas-signoz-data-protection-backup")]["spec"].get("storageClassName")


def assert_backup_storage_class() -> None:
    # The backup claim follows the SigNoz claim's storage class, so it binds where no default class exists.
    assert backup_claim_class(render_dev_with({})) is None
    docs = render_dev_with({"global": {"storageClass": "fast"}})
    assert backup_claim_class(docs) == "fast"
    sqlite = docs[("StatefulSet", "caritas-signoz")]["spec"]["volumeClaimTemplates"][0]["spec"]
    assert sqlite["storageClassName"] == "fast"
    assert backup_claim_class(render_dev_with({"signoz": {"signoz": {"persistence": {"storageClass": "ssd"}}}})) == "ssd"
    docs = render_dev_with({"global": {"storageClass": "-"}})
    assert backup_claim_class(docs) == ""
    assert docs[("StatefulSet", "caritas-signoz")]["spec"]["volumeClaimTemplates"][0]["spec"]["storageClassName"] == ""
    # An existing claim keeps its class: storageClassName cannot change on a bound claim.
    marker = '{{- $existingBackup := lookup "v1" "PersistentVolumeClaim" .Release.Namespace $backupPVC -}}'
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump({"global": {"storageClass": "fast"}}, handle)
    try:
        docs = by_key(render_with_cluster_state("values-dev.yaml", handle.name, release="caritas", replace={
            marker: '{{- $existingBackup := dict "spec" (dict "storageClassName" "local-path") -}}'}))
    finally:
        os.unlink(handle.name)
    assert backup_claim_class(docs) == "local-path"
    # An explicit empty class stays explicit; an absent one stays absent. Both are immutable.
    for existing, expected in (('(dict "storageClassName" "")', ""), ("dict", None)):
        docs = by_key(render_with_cluster_state("values-dev.yaml", release="caritas", replace={
            marker: f'{{{{- $existingBackup := dict "spec" {existing} -}}}}'}))
        assert backup_claim_class(docs) == expected, (existing, backup_claim_class(docs))


def assert_retain_hook_and_tolerations() -> None:
    # A 34+ character release name must keep the retain Job and its pod label within 63 characters.
    docs = render_with_pending_volume("values-dev.yaml", release="observability-staging-eu1-cluster-a")
    job = next(doc for doc in docs if doc["kind"] == "Job" and "retain" in doc["metadata"]["name"])
    name = job["metadata"]["name"]
    assert len(name) <= 63 and name.endswith("-retain"), name
    assert job["spec"]["template"]["metadata"]["labels"]["app"] == name
    accounts = {doc["metadata"]["name"] for doc in docs if doc["kind"] == "ServiceAccount"}
    assert job["spec"]["template"]["spec"]["serviceAccountName"] in accounts
    # The backup pod follows SigNoz onto a tainted node, so it carries SigNoz's tolerations.
    toleration = {"key": "dedicated", "operator": "Equal", "value": "observability", "effect": "NoSchedule"}
    docs = render_dev_with({"signoz": {"signoz": {"tolerations": [toleration]}}})
    cron = next(doc for kind, doc in docs.items() if kind[0] == "CronJob" and "data-protection" in kind[1])
    assert cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["tolerations"] == [toleration]
    signoz = docs[("StatefulSet", "caritas-signoz")]
    assert signoz["spec"]["template"]["spec"]["tolerations"] == [toleration]
    cron = next(doc for kind, doc in render_dev_with({}).items() if kind[0] == "CronJob" and "data-protection" in kind[1])
    assert "tolerations" not in cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]


def assert_web_vitals_need_signoz() -> None:
    # Without the bundled SigNoz the browser would get an empty metrics URL; the chart refuses instead.
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump({"global": {"observability": {"webVitals": {"enabled": True}}}, "signoz": {"enabled": False}}, handle)
    try:
        render(handle.name)
    except AssertionError as error:
        assert "webVitals.enabled=true requires signoz.enabled=true" in str(error), error
    else:
        raise AssertionError("web vitals rendered without SigNoz")
    finally:
        os.unlink(handle.name)


def assert_sqlite_replicas() -> None:
    # Every SigNoz replica's SQLite claim is retained; the backup mounts ordinal 0's and runs beside that pod.
    docs = render_dev_with({"signoz": {"signoz": {"replicaCount": 3}}})
    claims = retain_claims(docs)
    assert claims[:3] == [f"caritas/signoz-db-caritas-signoz-{n}" for n in range(3)], claims
    assert docs[("StatefulSet", "caritas-signoz")]["spec"]["replicas"] == 3
    cron = docs[("CronJob", "caritas-signoz-data-protection-backup")]
    pod = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    selector = pod["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]["labelSelector"]
    assert selector["matchLabels"]["statefulset.kubernetes.io/pod-name"] == "caritas-signoz-0", selector
    mounted = [v["persistentVolumeClaim"]["claimName"] for v in pod["volumes"] if "persistentVolumeClaim" in v]
    assert "signoz-db-caritas-signoz-0" in mounted, mounted
    docs = render_dev_with({"signoz": {"signoz": {"replicaCount": 2, "persistence": {"existingClaim": "shared-db"}}}})
    assert retain_claims(docs)[0] == "caritas/shared-db" and "caritas/shared-db" not in retain_claims(docs)[1:]


def assert_rum_port_and_source_class() -> None:
    # The RUM ingress needs the collector's RUM port and targets the port the Service really exposes.
    error = render_dev_fails({"signoz": {"otelCollector": {"ports": {"otlp-rum": {"enabled": False}}}}})
    assert "requires signoz.otelCollector.ports.otlp-rum.enabled=true" in error, error
    docs = render_dev_with({"signoz": {"otelCollector": {"ports": {"otlp-rum": {"servicePort": 4400}}}}})
    ingress = next(doc for kind, doc in docs.items() if kind[0] == "Ingress" and "rum" in kind[1])
    backend = ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    ports = docs[("Service", backend["name"])]["spec"]["ports"]
    assert backend["port"]["number"] in [port["port"] for port in ports if port["name"] == "otlp-rum"], (backend, ports)
    for bad in ("otlp-rum", 4319.5, 0, 70000):
        error = render_dev_fails({"signoz": {"otelCollector": {"ports": {"otlp-rum": {"servicePort": bad}}}}})
        assert "servicePort must be a port number" in error, (bad, error)
    # With no class configured, the backup claim takes the class the SQLite claim really has.
    marker = '{{- $sourceClaim := lookup "v1" "PersistentVolumeClaim" .Release.Namespace $sqliteClaim -}}'
    state = '{{- $sourceClaim := dict "spec" (dict "storageClassName" "pre-provisioned") -}}'
    docs = by_key(render_with_cluster_state("values-dev.yaml", release="caritas", replace={marker: state}))
    assert backup_claim_class(docs) == "pre-provisioned"
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump({"global": {"storageClass": "fast"}}, handle)
    try:
        docs = by_key(render_with_cluster_state("values-dev.yaml", handle.name, release="caritas", replace={marker: state}))
    finally:
        os.unlink(handle.name)
    assert backup_claim_class(docs) == "fast", "a configured class wins"


def assert_read_write_once_pod_rejected() -> None:
    # The backup pod mounts the SQLite claim beside SigNoz, which ReadWriteOncePod forbids.
    rwop = {"accessModes": ["ReadWriteOncePod"]}
    error = render_dev_fails({"signoz": {"signoz": {"persistence": rwop}}})
    assert "ReadWriteOncePod blocks it" in error, error
    render_dev_with({"signoz": {"signoz": {"persistence": rwop}, "orisoDataProtection": {"backup": {"enabled": False}}}})
    # An existingClaim is checked by what the cluster says about it.
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump({"signoz": {"signoz": {"persistence": {"existingClaim": "own-db"}}}}, handle)
    marker = '{{- $sqliteModes = dig "spec" "accessModes" $sqliteModes (lookup "v1" "PersistentVolumeClaim" .Release.Namespace $sqliteClaim) -}}'
    try:
        render_with_cluster_state("values-dev.yaml", handle.name, release="caritas", replace={
            marker: '{{- $sqliteModes = list "ReadWriteOncePod" -}}'})
    except AssertionError as error:
        assert "ReadWriteOncePod blocks it" in str(error), error
    else:
        raise AssertionError("a ReadWriteOncePod existingClaim rendered a backup")
    finally:
        os.unlink(handle.name)


def assert_backends_follow_signoz_names() -> None:
    # The ORISO ingresses and OTLP endpoints target the Services the vendored chart really names.
    for overlay, ui, collector in (
        ({}, "caritas-signoz", "caritas-signoz-otel-collector"),
        ({"signoz": {"fullnameOverride": "telemetry", "otelCollector": {"name": "collector"}}}, "telemetry", "telemetry-collector"),
        ({"signoz": {"nameOverride": "obs"}}, "caritas-obs", "caritas-obs-otel-collector"),
    ):
        docs = render_dev_with(overlay)
        services = {name for kind, name in docs if kind == "Service"}
        assert {ui, collector} <= services, (overlay, sorted(s for s in services if "signoz" in s or "obs" in s or "tele" in s))
        backends = {
            path["backend"]["service"]["name"]
            for kind, doc in docs.items() if kind[0] == "Ingress" and kind[1] in (f"{ui}-ingress", f"{ui}-rum-ingress")
            for rule in doc["spec"]["rules"] for path in rule["http"]["paths"]
        }
        assert {ui, collector} <= backends, (overlay, backends)
        endpoints = {
            value["value"].split("//")[-1].split(":")[0].split(".")[0]
            for kind, doc in docs.items() if kind[0] in ("Deployment", "StatefulSet")
            for container in doc["spec"]["template"]["spec"].get("containers", [])
            for value in container.get("env", []) or []
            if isinstance(value.get("value"), str) and ":4318" in value["value"]
        }
        assert endpoints and endpoints <= services, (overlay, endpoints)


def clickhouse_resources(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        container.get("resources", {})
        for doc in docs
        if doc.get("kind") == "ClickHouseInstallation"
        for template in doc["spec"]["templates"]["podTemplates"]
        for container in template["spec"]["containers"]
        if container["name"] == "clickhouse"
    ]


def assert_clickhouse_memory_on_dev() -> None:
    # A limit stops ClickHouse taking the whole worker; the request lets the
    # scheduler see what it really uses.
    assert clickhouse_resources(render("values-dev.yaml")) == [{
        "requests": {"cpu": "100m", "memory": "2Gi"},
        "limits": {"memory": "4Gi"},
    }]


def main() -> None:
    assert_dev(by_key(render("values-dev.yaml")))
    assert_default_off(by_key(render()))
    assert_review_findings()
    assert_second_review_findings()
    assert_e2e_trace_ingest()
    assert_e2e_trace_ingest_scope()
    assert_backup_is_atomic()
    assert_long_release_name()
    assert_name_override_findings()
    assert_retain_hook_and_tolerations()
    assert_backup_storage_class()
    assert_web_vitals_need_signoz()
    assert_sqlite_replicas()
    assert_rum_port_and_source_class()
    assert_read_write_once_pod_rejected()
    assert_backends_follow_signoz_names()
    assert_clickhouse_memory_on_dev()
    print("PASS: SigNoz on Dev keeps its data, bounds the collector, and labels every signal")


if __name__ == "__main__":
    main()
