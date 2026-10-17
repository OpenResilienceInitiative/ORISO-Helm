#!/usr/bin/env python3
"""Render contract for SigNoz on Dev: data protection, collector hardening,
trace identity and sampling, browser Web Vitals (ORISO SigNoz plan, session 1)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Any

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAVA_SERVICES = ("userservice", "agencyservice", "tenantservice", "consultingtypeservice")


def render(*overlays: str, release: str = "caritas", chart: str = CHART_DIR) -> list[dict[str, Any]]:
    command = ["helm", "template", release, chart, "--namespace", "caritas"]
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
    # Run the rendered backup script on a real SQLite file in a scratch dir.
    import gzip
    import sqlite3
    config = by_key(render("values-dev.yaml"))[("ConfigMap", "caritas-signoz-data-protection")]
    with tempfile.TemporaryDirectory() as root:
        signoz, backup = os.path.join(root, "signoz"), os.path.join(root, "backup")
        os.makedirs(signoz)
        os.makedirs(backup)
        db = sqlite3.connect(os.path.join(signoz, "signoz.db"))
        db.execute("create table users (name text)")
        db.execute("insert into users values ('admin')")
        db.commit()
        db.close()
        stale = os.path.join(backup, ".signoz-20261017T000000Z.db.gz.part")
        with open(stale, "wb") as handle:
            handle.write(b"truncated")
        script = config["data"]["backup.py"].replace("/signoz/", signoz + "/").replace("/backup", backup)
        result = subprocess.run(["python3", "-c", script], env={**os.environ, "KEEP": "14"},
                                capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        files = sorted(os.listdir(backup))
        assert len(files) == 1 and files[0].startswith("signoz-") and files[0].endswith(".db.gz"), files
        restored = os.path.join(root, "restored.db")
        with gzip.open(os.path.join(backup, files[0]), "rb") as source, open(restored, "wb") as target:
            target.write(source.read())
        assert sqlite3.connect(restored).execute("select name from users").fetchall() == [("admin",)]


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
    marker = '{{- $existingBackup := lookup "v1" "PersistentVolumeClaim" .Release.Namespace (printf "%s-backup" $name) -}}'
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump({"global": {"storageClass": "fast"}}, handle)
    try:
        docs = by_key(render_with_cluster_state("values-dev.yaml", handle.name, release="caritas", replace={
            marker: '{{- $existingBackup := dict "spec" (dict "storageClassName" "local-path") -}}'}))
    finally:
        os.unlink(handle.name)
    assert backup_claim_class(docs) == "local-path"


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


def main() -> None:
    assert_dev(by_key(render("values-dev.yaml")))
    assert_default_off(by_key(render()))
    assert_review_findings()
    assert_second_review_findings()
    assert_backup_is_atomic()
    assert_long_release_name()
    assert_name_override_findings()
    assert_retain_hook_and_tolerations()
    assert_backup_storage_class()
    assert_web_vitals_need_signoz()
    assert_sqlite_replicas()
    assert_rum_port_and_source_class()
    assert_read_write_once_pod_rejected()
    print("PASS: SigNoz on Dev keeps its data, bounds the collector, and labels every signal")


if __name__ == "__main__":
    main()
