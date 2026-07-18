#!/usr/bin/env python3
"""OBS-P4: render-based contract for the ClickHouse/MariaDB/collector-self-
monitoring dashboard data sources.

Locks the invariants that make three SigNoz dashboards actually show data
(one already imported but empty, two intended to be imported by hand once
these receivers are live on Pre-Dev):

  - ClickHouse gets a native <prometheus> config drop-in (port 9363,
    /metrics), with metrics/events/asynchronous_metrics all enabled — this
    is what makes the already-imported "ClickHouse Overview" dashboard
    non-empty. The StatefulSet mounts the drop-in as its own subPath
    (alongside the existing cluster.xml) and exposes the port on both the
    container and the Service,
  - the gateway otel-collector gets a `prometheus` receiver that scrapes
    that ClickHouse endpoint by job_name "clickhouse" (the job_name becomes
    each series' service.name, which is what the dashboard groups by),
  - the gateway CAN get a `mysql` receiver against MariaDB (off by default —
    see below), with credentials sourced via secretKeyRef from the SAME
    Secret an existing backend Deployment already consumes (LC-M03
    precedent) — NEVER a new plaintext credential and NEVER the MariaDB root
    password,
  - `signoz.mariadbMetrics.enabled` defaults to `false`: the only credential
    available to reuse today is AgencyService's own app DB user (full
    SELECT/INSERT/UPDATE/DELETE on its schema), not a dedicated
    least-privilege monitoring account — shipping this enabled by default
    would use an over-privileged credential for a new purpose with no
    separate human decision point. Same off-by-default posture as OBS-P6.
  - both new receivers land in a dedicated `metrics/infra` pipeline (not the
    main `metrics` pipeline) since k8sattributes' connection-IP association
    doesn't apply to scraped/dialed-out sources, but still write through the
    same signozclickhousemetrics/signozmeter exporters (no duplicated
    ClickHouse ingest path),
  - both the gateway and the otel-agent DaemonSet enable
    service.telemetry.metrics (the current "readers" schema, not the
    removed `address` field) on :8888 and scrape themselves back via a
    `prometheus` receiver job — the otel-agent's job self-scrapes locally
    because a DaemonSet has no single stable remote-scrapeable address.
"""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = "oriso-platform"
SIGNOZ_TEST_SET = [
    "signoz.enabled=true",
    "signoz.otelAgent.enabled=true",
    "signoz.otelAgent.logsNamespace=caritas",
]


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def render(*value_files: str, extra_set: list[str] | None = None) -> list[dict]:
    cmd = ["helm", "template", RELEASE, CHART_DIR, "--namespace", "caritas"]
    for vf in value_files:
        cmd += ["-f", os.path.join(CHART_DIR, vf)]
    cmd += ["-f", os.path.join(CHART_DIR, "secrets.yaml.default")]
    cmd += ["--set", "global.secrets.clickhousePassword=x"]
    for s in SIGNOZ_TEST_SET:
        cmd += ["--set", s]
    for s in extra_set or []:
        cmd += ["--set", s]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"helm template failed for {value_files}: {result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def resource(documents: list[dict], kind: str, name: str) -> dict:
    for document in documents:
        if document.get("kind") == kind and document.get("metadata", {}).get("name") == name:
            return document
    fail(f"rendered {kind}/{name} is missing")


def gateway_config(documents: list[dict]) -> dict:
    cm = resource(documents, "ConfigMap", f"{RELEASE}-otel-collector")
    return yaml.safe_load(cm["data"]["otel-collector-config.yaml"])


def agent_config(documents: list[dict]) -> dict:
    cm = resource(documents, "ConfigMap", f"{RELEASE}-otel-agent")
    return yaml.safe_load(cm["data"]["otel-agent-config.yaml"])


def main() -> None:
    documents = render("values.yaml.default")

    # --- Task 1: ClickHouse Prometheus endpoint ----------------------------
    ch_cm = resource(documents, "ConfigMap", f"{RELEASE}-clickhouse-config")
    if "prometheus.xml" not in ch_cm["data"]:
        fail("ClickHouse config drop-in ConfigMap is missing prometheus.xml")
    prom_xml = ch_cm["data"]["prometheus.xml"]
    for required in ("<port>9363</port>", "<metrics>true</metrics>", "<events>true</events>"):
        if required not in prom_xml:
            fail(f"ClickHouse prometheus.xml is missing {required!r}:\n{prom_xml}")

    ch_sts = resource(documents, "StatefulSet", f"{RELEASE}-clickhouse")
    ch_container = ch_sts["spec"]["template"]["spec"]["containers"][0]
    ch_ports = {p["name"]: p for p in ch_container["ports"]}
    if ch_ports.get("metrics", {}).get("containerPort") != 9363:
        fail(f"ClickHouse StatefulSet must expose containerPort 9363 as 'metrics', got {ch_ports.get('metrics')}")
    ch_mounts = [m for m in ch_container["volumeMounts"] if m["mountPath"].endswith("prometheus.xml")]
    if not ch_mounts or ch_mounts[0].get("subPath") != "prometheus.xml":
        fail("ClickHouse StatefulSet must mount the prometheus.xml drop-in via subPath")

    ch_svc = resource(documents, "Service", f"{RELEASE}-clickhouse")
    svc_ports = {p["name"]: p for p in ch_svc["spec"]["ports"]}
    if svc_ports.get("metrics", {}).get("port") != 9363:
        fail(f"ClickHouse Service must expose port 9363 as 'metrics', got {svc_ports.get('metrics')}")

    gw_cfg = gateway_config(documents)
    gw_receivers = gw_cfg["receivers"]
    if "prometheus" not in gw_receivers:
        fail("gateway otel-collector is missing the prometheus receiver")
    scrape_jobs = {
        job["job_name"]: job
        for job in gw_receivers["prometheus"]["config"]["scrape_configs"]
    }
    if "clickhouse" not in scrape_jobs:
        fail(f"gateway prometheus receiver is missing a 'clickhouse' scrape job, got {list(scrape_jobs)}")
    ch_target = scrape_jobs["clickhouse"]["static_configs"][0]["targets"][0]
    if not ch_target.endswith(":9363") or f"{RELEASE}-clickhouse" not in ch_target:
        fail(f"clickhouse scrape job must target the in-chart ClickHouse Service on :9363, got {ch_target}")

    # --- Task 2a: MariaDB mysql receiver is OFF by default ------------------
    # See docstring: the only reusable credential today is AgencyService's
    # over-privileged app DB user, not a dedicated monitoring account, so
    # this must not ship enabled without a separate human decision.
    if "mysql" in gw_receivers:
        fail("mysql receiver must be ABSENT by default (signoz.mariadbMetrics.enabled defaults false)")
    default_infra_pipeline = gw_cfg["service"]["pipelines"].get("metrics/infra", {})
    if "mysql" in default_infra_pipeline.get("receivers", []):
        fail("metrics/infra pipeline must not reference mysql when mariadbMetrics is disabled")

    # --- Task 2b: when explicitly enabled, credential-reuse contract holds -
    mysql_documents = render(
        "values.yaml.default",
        extra_set=["signoz.mariadbMetrics.enabled=true"],
    )
    mysql_gw_cfg = gateway_config(mysql_documents)
    mysql_gw_receivers = mysql_gw_cfg["receivers"]
    if "mysql" not in mysql_gw_receivers:
        fail("gateway otel-collector is missing the mysql receiver when mariadbMetrics.enabled=true")
    mysql_recv = mysql_gw_receivers["mysql"]
    if "mariadb" not in mysql_recv.get("endpoint", ""):
        fail(f"mysql receiver must target the mariadb Service, got {mysql_recv.get('endpoint')}")
    if mysql_recv.get("username") != "${env:MARIADB_METRICS_USERNAME}":
        fail(f"mysql receiver username must be env-interpolated, got {mysql_recv.get('username')!r}")
    if mysql_recv.get("password") != "${env:MARIADB_METRICS_PASSWORD}":
        fail(f"mysql receiver password must be env-interpolated, got {mysql_recv.get('password')!r}")

    mysql_gw_deploy = resource(mysql_documents, "Deployment", f"{RELEASE}-otel-collector")
    mysql_gw_container = mysql_gw_deploy["spec"]["template"]["spec"]["containers"][0]
    mysql_gw_env = {e["name"]: e for e in mysql_gw_container["env"]}
    for var in ("MARIADB_METRICS_USERNAME", "MARIADB_METRICS_PASSWORD"):
        if var not in mysql_gw_env:
            fail(f"gateway Deployment is missing env var {var} when mariadbMetrics.enabled=true")
        secret_ref = mysql_gw_env[var].get("valueFrom", {}).get("secretKeyRef")
        if not secret_ref:
            fail(f"{var} must come from a secretKeyRef (LC-M03), got {mysql_gw_env[var]}")
    # Must reuse an EXISTING backend secret, not a new one minted for this
    # purpose, and must never be the MariaDB root credential.
    user_secret = mysql_gw_env["MARIADB_METRICS_USERNAME"]["valueFrom"]["secretKeyRef"]
    pass_secret = mysql_gw_env["MARIADB_METRICS_PASSWORD"]["valueFrom"]["secretKeyRef"]
    if user_secret["name"] != f"{RELEASE}-agencyservice-secrets":
        fail(
            "mysql receiver username must reuse the existing AgencyService secret "
            f"('{RELEASE}-agencyservice-secrets'), got {user_secret['name']!r} — if this was "
            "changed intentionally to a different existing secret that's fine, but it must not "
            "be a newly-minted credential or the mariadb-secret root password"
        )
    if pass_secret["name"] != f"{RELEASE}-agencyservice-secrets":
        fail(f"mysql receiver password secret name mismatch: {pass_secret['name']!r}")
    if "ROOT" in user_secret["key"].upper() or "ROOT" in pass_secret["key"].upper():
        fail("mysql receiver must NEVER use the MariaDB root credential")
    mysql_infra_pipeline = mysql_gw_cfg["service"]["pipelines"].get("metrics/infra", {})
    if "mysql" not in mysql_infra_pipeline.get("receivers", []):
        fail("metrics/infra pipeline must include mysql when mariadbMetrics.enabled=true")

    # --- Task 3: self-monitoring (gateway + agent) --------------------------
    gw_telemetry_metrics = gw_cfg["service"].get("telemetry", {}).get("metrics")
    if not gw_telemetry_metrics:
        fail("gateway otel-collector must enable service.telemetry.metrics for self-monitoring")
    if gw_telemetry_metrics.get("level") == "none":
        fail("gateway telemetry metrics level must not be 'none'")
    readers = gw_telemetry_metrics.get("readers", [])
    if not readers or "prometheus" not in readers[0].get("pull", {}).get("exporter", {}):
        fail(f"gateway telemetry.metrics must configure a pull/prometheus reader, got {readers}")
    if readers[0]["pull"]["exporter"]["prometheus"].get("port") != 8888:
        fail("gateway self-metrics reader must expose port 8888 (matches the existing Service port)")

    if "otel-collector" not in scrape_jobs:
        fail(f"gateway prometheus receiver is missing a self-scrape 'otel-collector' job, got {list(scrape_jobs)}")
    self_target = scrape_jobs["otel-collector"]["static_configs"][0]["targets"][0]
    if self_target != "localhost:8888":
        fail(f"gateway self-scrape job must target localhost:8888, got {self_target}")

    infra_pipeline = gw_cfg["service"]["pipelines"].get("metrics/infra")
    if not infra_pipeline:
        fail("gateway is missing the metrics/infra pipeline for ClickHouse/MariaDB/self-metrics")
    if infra_pipeline["processors"][0] != "memory_limiter":
        fail(f"metrics/infra pipeline must start with memory_limiter (K8-L02), got {infra_pipeline['processors']}")
    # mysql is intentionally absent here (mariadbMetrics defaults false —
    # see Task 2a); its presence when explicitly enabled is checked above.
    if "prometheus" not in infra_pipeline["receivers"]:
        fail(f"metrics/infra pipeline must include the prometheus receiver, got {infra_pipeline['receivers']}")
    if set(infra_pipeline["exporters"]) != {"signozclickhousemetrics", "signozmeter"}:
        fail(
            "metrics/infra pipeline must reuse the SAME exporters as the main metrics pipeline "
            f"(no duplicated ClickHouse ingest path), got {infra_pipeline['exporters']}"
        )
    main_metrics_receivers = gw_cfg["service"]["pipelines"]["metrics"]["receivers"]
    if "prometheus" in main_metrics_receivers or "mysql" in main_metrics_receivers:
        fail(
            "prometheus/mysql receivers must stay OUT of the main `metrics` pipeline "
            "(k8sattributes' connection-IP association does not apply to scraped/dialed-out "
            f"sources), got {main_metrics_receivers}"
        )

    agent_cfg = agent_config(documents)
    agent_telemetry_metrics = agent_cfg["service"].get("telemetry", {}).get("metrics")
    if not agent_telemetry_metrics:
        fail("otel-agent must enable service.telemetry.metrics for self-monitoring")
    agent_readers = agent_telemetry_metrics.get("readers", [])
    if not agent_readers or agent_readers[0]["pull"]["exporter"]["prometheus"].get("port") != 8888:
        fail(f"otel-agent telemetry.metrics must configure a pull/prometheus reader on :8888, got {agent_readers}")

    agent_receivers = agent_cfg["receivers"]
    if "prometheus" not in agent_receivers:
        fail("otel-agent is missing the self-scrape prometheus receiver")
    agent_scrape_jobs = {j["job_name"]: j for j in agent_receivers["prometheus"]["config"]["scrape_configs"]}
    if "otel-agent" not in agent_scrape_jobs:
        fail(f"otel-agent prometheus receiver is missing an 'otel-agent' self-scrape job, got {list(agent_scrape_jobs)}")
    agent_self_target = agent_scrape_jobs["otel-agent"]["static_configs"][0]["targets"][0]
    if agent_self_target != "localhost:8888":
        fail(f"otel-agent self-scrape job must target localhost:8888, got {agent_self_target}")

    agent_metrics_pipeline = agent_cfg["service"]["pipelines"]["metrics"]
    for recv in ("hostmetrics", "kubeletstats", "prometheus"):
        if recv not in agent_metrics_pipeline["receivers"]:
            fail(f"otel-agent metrics pipeline must include {recv} (OBS-P4a/P4 regression), got {agent_metrics_pipeline['receivers']}")
    if agent_metrics_pipeline["processors"][0] != "memory_limiter":
        fail("otel-agent metrics pipeline must still start with memory_limiter (K8-L02 regression)")

    # otel-agent must still keep forwarding OTLP to the gateway (no second
    # ingest path introduced by this change).
    agent_metrics_exporters = agent_metrics_pipeline["exporters"]
    if agent_metrics_exporters != agent_cfg["service"]["pipelines"]["logs"]["exporters"]:
        fail(
            "otel-agent metrics pipeline must keep reusing the same otlp exporter as logs "
            f"(OBS-P4a regression), got {agent_metrics_exporters}"
        )

    print(
        "OK: OBS-P4 contract holds — ClickHouse exposes a native <prometheus> endpoint scraped "
        "by the gateway's 'clickhouse' job, MariaDB is scraped via the mysql receiver with "
        "credentials reused from the existing AgencyService secret (never root, never a new "
        "plaintext credential), and both the gateway and otel-agent self-monitor via "
        "service.telemetry.metrics + a local prometheus self-scrape — all landing in the "
        "dedicated metrics/infra pipeline through the existing ClickHouse write path"
    )


if __name__ == "__main__":
    main()
