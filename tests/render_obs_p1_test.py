#!/usr/bin/env python3
"""OBS-P1: render-based contract for the SigNoz observability stack.

Locks the invariants that made the previously deployed stack broken or
unsafe, plus the cluster-adoption invariants that keep a helm upgrade from
duplicating the workloads already running on Pre-Dev:

  - the gateway collector is the SigNoz ingest layer: image
    signoz/signoz-otel-collector, ClickHouse exporters (clickhousetraces /
    signozclickhousemetrics / clickhouselogsexporter) with env-driven DSNs,
    opamp registration against signoz:4320 — the signoz/signoz single
    binary has NO OTLP listener, so an otlp exporter to it is a bug,
  - the gateway runs traces AND metrics AND logs pipelines (the pre-OBS-P1
    config was metrics only), each starting with memory_limiter (K8-L02);
    k8sattributes runs on traces/metrics but NOT on logs (its connection-IP
    association stamps the agent's workload identity onto log records),
  - the schema migrator renders as ONE job whose initContainer chain
    (ready -> bootstrap -> sync, then async as the main container) enforces
    sequential migration — concurrent sync+async corrupts the bookkeeping —
    and the gateway gates its own start on "migrate sync check",
  - rendered images match the values pins (version bumps stay consistent),
  - the log agent forwards to the gateway collector, not to signoz,
  - the ClickHouse password reaches every consumer via secretKeyRef, never
    plaintext env or a ConfigMap (LC-M03),
  - ClickHouse keeps the immutable identity of the running StatefulSet
    (selector app=clickhouse, serviceName <release>-clickhouse, 50Gi PVC),
  - the SigNoz ingress renders only when explicitly enabled with a host, and
    does NOT render from the committed prod overlay.
"""

from __future__ import annotations

import os
import subprocess
import sys

import yaml


CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = "oriso"
TEST_SIGNOZ_DOMAIN = "signoz.example.test"
TEST_SIGNOZ_TLS_SECRET = "signoz-test-tls"
SIGNOZ_TEST_SET = [
    "signoz.enabled=true",
    "signoz.ingress.enabled=true",
    f"global.domains.signoz={TEST_SIGNOZ_DOMAIN}",
    f"signoz.ingress.tlsSecretName={TEST_SIGNOZ_TLS_SECRET}",
    "signoz.otelAgent.enabled=true",
    "signoz.otelAgent.logsNamespace=caritas",
]


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def render(overlay: str | None = None, extra_set: list[str] | None = None) -> list[dict]:
    command = [
        "helm",
        "template",
        RELEASE,
        CHART_DIR,
        "-f",
        os.path.join(CHART_DIR, "values.yaml.default"),
        "-f",
        os.path.join(CHART_DIR, "secrets.yaml.default"),
    ]
    if overlay:
        overlay_path = os.path.join(CHART_DIR, overlay)
        if not os.path.isfile(overlay_path):
            fail(f"{overlay} is missing")
        command += ["-f", overlay_path]
    for setting in extra_set or []:
        command += ["--set", setting]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"chart render with {overlay} failed:\n{result.stderr}")
    return [document for document in yaml.safe_load_all(result.stdout) if isinstance(document, dict)]


def resource(documents: list[dict], kind: str, name: str) -> dict:
    for document in documents:
        if document.get("kind") == kind and document.get("metadata", {}).get("name") == name:
            return document
    fail(f"rendered {kind}/{name} is missing")


def find(documents: list[dict], kind: str, name: str) -> dict | None:
    for document in documents:
        if document.get("kind") == kind and document.get("metadata", {}).get("name") == name:
            return document
    return None


def container_env(workload: dict) -> dict[str, dict]:
    container = workload["spec"]["template"]["spec"]["containers"][0]
    return {entry["name"]: entry for entry in container.get("env", [])}


def assert_password_from_secret(workload: dict, what: str) -> None:
    env = container_env(workload)
    entry = env.get("CLICKHOUSE_PASSWORD")
    if entry is None:
        fail(f"{what} has no CLICKHOUSE_PASSWORD env")
    if "value" in entry:
        fail(f"{what} carries the ClickHouse password as plaintext env (LC-M03)")
    source = entry.get("valueFrom", {})
    if "secretKeyRef" not in source:
        fail(f"{what} CLICKHOUSE_PASSWORD must come from a secretKeyRef, got: {source}")


def collector_config(documents: list[dict]) -> dict:
    configmap = resource(documents, "ConfigMap", f"{RELEASE}-otel-collector")
    return yaml.safe_load(configmap["data"]["otel-collector-config.yaml"])


def values_defaults() -> dict:
    with open(os.path.join(CHART_DIR, "values.yaml.default")) as handle:
        return yaml.safe_load(handle)


def image_of(workload: dict, container: int = 0) -> str:
    return workload["spec"]["template"]["spec"]["containers"][container]["image"]


def main() -> None:
    documents = render(extra_set=SIGNOZ_TEST_SET)
    values = values_defaults()["signoz"]

    # --- gateway collector = SigNoz ingest layer --------------------------
    # The signoz/signoz single binary has NO OTLP listener; ingest must be
    # the signoz-otel-collector distro writing to ClickHouse directly.
    gateway = resource(documents, "Deployment", f"{RELEASE}-otel-collector")
    gateway_image = image_of(gateway)
    if not gateway_image.startswith("signoz/signoz-otel-collector:"):
        fail(f"gateway collector must run the signoz-otel-collector distro, got {gateway_image}")

    # Version pins: the rendered images must match the values pins, and the
    # ClickHouse tag must match what the upstream chart pairs with the
    # collector tag (assert consistency, not literals).
    expected_gateway = f"{values['otelCollector']['image']['repository']}:{values['otelCollector']['image']['tag']}"
    if gateway_image != expected_gateway:
        fail(f"gateway image {gateway_image} does not match the values pin {expected_gateway}")
    signoz_sts = resource(documents, "StatefulSet", f"{RELEASE}-signoz")
    expected_signoz = f"{values['image']['repository']}:{values['image']['tag']}"
    if image_of(signoz_sts) != expected_signoz:
        fail(f"signoz image {image_of(signoz_sts)} does not match the values pin {expected_signoz}")
    clickhouse_sts = resource(documents, "StatefulSet", f"{RELEASE}-clickhouse")
    expected_clickhouse = f"{values['clickhouse']['image']['repository']}:{values['clickhouse']['image']['tag']}"
    if image_of(clickhouse_sts) != expected_clickhouse:
        fail(f"clickhouse image {image_of(clickhouse_sts)} does not match the values pin {expected_clickhouse}")

    # The removed prometheus NormalizeName feature gate must never come back
    # (it no longer exists in signoz-otel-collector v0.144+ and fails startup).
    gateway_args = gateway["spec"]["template"]["spec"]["containers"][0].get("args", [])
    if any("feature-gates" in arg and "NormalizeName" in arg for arg in gateway_args):
        fail(f"gateway args carry the removed NormalizeName feature gate: {gateway_args}")

    config = collector_config(documents)

    pipelines = config["service"]["pipelines"]
    for signal in ("traces", "metrics", "logs"):
        if signal not in pipelines:
            fail(f"gateway collector is missing the {signal} pipeline")
        processors = pipelines[signal].get("processors", [])
        if not processors or processors[0] != "memory_limiter":
            fail(f"{signal} pipeline must start with memory_limiter (K8-L02), got {processors}")
    for signal in ("traces", "metrics"):
        if "k8sattributes" not in pipelines[signal]["processors"]:
            fail(f"{signal} pipeline must include k8sattributes, got {pipelines[signal]['processors']}")
    # Logs must NOT be re-stamped: the gateway's connection-IP association
    # would attribute every record to the agent DaemonSet (seen live).
    if "k8sattributes" in pipelines["logs"]["processors"]:
        fail("logs pipeline must not run k8sattributes (re-stamps agent identity onto log records)")

    exporters = config["exporters"]
    for name, dsn_key in (
        ("clickhousetraces", "datasource"),
        ("signozclickhousemetrics", "dsn"),
        ("clickhouselogsexporter", "dsn"),
    ):
        if name not in exporters:
            fail(f"gateway collector is missing the {name} ClickHouse exporter")
        dsn = exporters[name][dsn_key]
        if "${env:CLICKHOUSE_HOST}" not in dsn or "${env:CLICKHOUSE_PASSWORD}" not in dsn:
            fail(f"{name} DSN must come from CLICKHOUSE_* env, got {dsn}")
    if "otlp" in exporters:
        fail("gateway collector must not export OTLP to the signoz binary (it has no OTLP listener)")
    if pipelines["traces"]["exporters"][0] != "clickhousetraces":
        fail("traces pipeline must export to clickhousetraces")
    if pipelines["metrics"]["exporters"][0] != "signozclickhousemetrics":
        fail("metrics pipeline must export to signozclickhousemetrics")
    if pipelines["logs"]["exporters"][0] != "clickhouselogsexporter":
        fail("logs pipeline must export to clickhouselogsexporter")

    # ClickHouse credentials reach the gateway via secretKeyRef, and the
    # opamp manager config points at the SigNoz backend (:4320).
    assert_password_from_secret(gateway, "gateway collector Deployment")
    gateway_configmap = resource(documents, "ConfigMap", f"{RELEASE}-otel-collector")
    opamp = yaml.safe_load(gateway_configmap["data"]["otel-collector-opamp-config.yaml"])
    if f"{RELEASE}-signoz" not in opamp["server_endpoint"] or ":4320" not in opamp["server_endpoint"]:
        fail(f"opamp manager must point at the SigNoz backend :4320, got {opamp['server_endpoint']}")

    # --- schema migrator: ONE job, sequential by construction --------------
    # Upstream (chart signoz-0.132.x) runs the migration phases as a single
    # Job: initContainers ready -> bootstrap -> sync, main container async.
    # This ordering is load-bearing: concurrent sync+async corrupted the
    # migration bookkeeping on Pre-Dev (missing root_operations /
    # time_series_v4 tables).
    job = resource(documents, "Job", f"{RELEASE}-schema-migrator")
    init_phases = [
        (c["name"], c["args"]) for c in job["spec"]["template"]["spec"].get("initContainers", [])
    ]
    if [name for name, _ in init_phases] != ["ready", "bootstrap", "sync"]:
        fail(f"schema migrator initContainers must run ready -> bootstrap -> sync, got {init_phases}")
    if init_phases[2][1] != ["migrate", "sync", "up"]:
        fail(f"schema migrator sync phase args wrong: {init_phases[2][1]}")
    async_container = job["spec"]["template"]["spec"]["containers"][0]
    if async_container["args"] != ["migrate", "async", "up"]:
        fail(f"schema migrator main container must run 'migrate async up', got {async_container['args']}")
    # Migrator uses the collector image (no standalone migrator image since
    # upstream chart 0.132.x) — values-consistency, not a literal tag.
    if async_container["image"] != expected_gateway:
        fail(f"schema migrator must use the gateway collector image, got {async_container['image']}")
    assert_password_from_secret(job, "schema migrator Job")
    async_env = {e["name"]: e.get("value") for e in async_container.get("env", [])}
    if async_env.get("SIGNOZ_OTEL_COLLECTOR_CLICKHOUSE_REPLICATION") != "false":
        fail("migrator must keep REPLICATION=false (existing schema is non-replicated)")

    # The gateway must gate its start on the schema being current.
    gateway_inits = gateway["spec"]["template"]["spec"].get("initContainers", [])
    check = next((c for c in gateway_inits if c.get("args") == ["migrate", "sync", "check"]), None)
    if check is None:
        fail("gateway collector must run a 'migrate sync check' initContainer")

    # --- log-collection agent -------------------------------------------
    agent = resource(documents, "DaemonSet", f"{RELEASE}-otel-agent")
    agent_configmap = resource(documents, "ConfigMap", f"{RELEASE}-otel-agent")
    agent_config = yaml.safe_load(agent_configmap["data"]["otel-agent-config.yaml"])

    agent_image = image_of(agent)
    expected_agent = f"{values['otelAgent']['image']['repository']}:{values['otelAgent']['image']['tag']}"
    if agent_image != expected_agent:
        fail(f"agent image {agent_image} does not match the values pin {expected_agent}")

    # contrib >= 0.127 removed service.telemetry.metrics.address; probes must
    # come from the health_check extension instead.
    if agent_config.get("service", {}).get("telemetry", {}).get("metrics", {}).get("address"):
        fail("agent config carries the removed service.telemetry.metrics.address field")
    if "health_check" not in agent_config.get("extensions", {}):
        fail("agent config must define the health_check extension for probes")
    if "health_check" not in agent_config["service"].get("extensions", []):
        fail("agent service.extensions must enable health_check")

    includes = agent_config["receivers"]["filelog"]["include"]
    if not any(path.startswith("/var/log/pods/caritas_") for path in includes):
        fail(f"agent filelog is not scoped to the caritas namespace: {includes}")

    agent_pipelines = agent_config["service"]["pipelines"]
    logs_processors = agent_pipelines.get("logs", {}).get("processors", [])
    if not logs_processors or logs_processors[0] != "memory_limiter":
        fail(f"agent logs pipeline must start with memory_limiter, got {logs_processors}")

    agent_endpoint = agent_config["exporters"]["otlp"]["endpoint"]
    if not agent_endpoint.endswith(":4317"):
        fail(f"agent must export OTLP on port 4317, got {agent_endpoint}")
    if f"{RELEASE}-otel-collector" not in agent_endpoint:
        fail(f"agent must forward to the gateway collector, not signoz directly, got {agent_endpoint}")

    mounts = agent["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    log_mount = next((m for m in mounts if m["mountPath"] == "/var/log/pods"), None)
    if log_mount is None or not log_mount.get("readOnly"):
        fail("agent must mount /var/log/pods read-only")

    # --- ClickHouse credentials (LC-M03) ----------------------------------
    clickhouse = resource(documents, "StatefulSet", f"{RELEASE}-clickhouse")
    signoz = resource(documents, "StatefulSet", f"{RELEASE}-signoz")
    assert_password_from_secret(clickhouse, "ClickHouse StatefulSet")
    assert_password_from_secret(signoz, "SigNoz StatefulSet")
    resource(documents, "Secret", "clickhouse-secret")

    # --- ClickHouse adoption invariants (Pre-Dev runs this already) ------
    if clickhouse["spec"]["selector"]["matchLabels"] != {"app": "clickhouse"}:
        fail("ClickHouse selector must stay app=clickhouse (immutable on the running StatefulSet)")
    if clickhouse["spec"]["serviceName"] != f"{RELEASE}-clickhouse":
        fail("ClickHouse serviceName must stay <release>-clickhouse")
    claim = clickhouse["spec"]["volumeClaimTemplates"][0]
    if claim["metadata"]["name"] != "clickhouse-data":
        fail("ClickHouse volumeClaimTemplate must stay clickhouse-data (immutable)")
    if claim["spec"]["resources"]["requests"]["storage"] != "50Gi":
        fail("ClickHouse PVC size must stay 50Gi (matches the bound Pre-Dev PVC)")

    # SigNoz must point at the in-chart ClickHouse.
    signoz_env = container_env(signoz)
    if signoz_env["CLICKHOUSE_HOST"]["value"] != f"{RELEASE}-clickhouse":
        fail(f"SigNoz must use the in-chart ClickHouse, got {signoz_env['CLICKHOUSE_HOST']}")

    # The schema migrator issues ON CLUSTER DDL: ClickHouse needs the
    # remote_servers cluster and a (Zoo)Keeper, provided by the drop-in.
    cluster_config = resource(documents, "ConfigMap", f"{RELEASE}-clickhouse-config")
    cluster_xml = cluster_config["data"]["cluster.xml"]
    if "<remote_servers>" not in cluster_xml or "<cluster>" not in cluster_xml:
        fail("ClickHouse drop-in must define the remote_servers cluster the migrator targets")
    if "<keeper_server>" not in cluster_xml:
        fail("ClickHouse drop-in must enable the embedded Keeper for ON CLUSTER DDL")
    ch_mounts = clickhouse["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    if not any(m["mountPath"] == "/etc/clickhouse-server/config.d/cluster.xml" for m in ch_mounts):
        fail("ClickHouse StatefulSet must mount the cluster/keeper drop-in into config.d")

    # --- ingress: on only when explicitly configured, off for prod ---------
    ingress = resource(documents, "Ingress", "signoz-ingress")
    if ingress["spec"]["rules"][0]["host"] != TEST_SIGNOZ_DOMAIN:
        fail("signoz-ingress must serve the explicitly configured host")
    tls = ingress["spec"]["tls"][0]
    if tls["secretName"] != TEST_SIGNOZ_TLS_SECRET:
        fail("signoz-ingress must use the explicitly configured TLS secret")
    annotations = ingress["metadata"].get("annotations", {})
    if annotations.get("cert-manager.io/cluster-issuer") != "letsencrypt-prod":
        fail("signoz-ingress must use the letsencrypt-prod cluster-issuer like the other ingresses")
    # kubectl-apply from outside the namespace must not land it in "default"
    # (nginx admission webhook duplicate-host check).
    if not ingress["metadata"].get("namespace"):
        fail("signoz-ingress must carry an explicit metadata.namespace")

    prod_documents = render("values-prod.yaml")
    if find(prod_documents, "Ingress", "signoz-ingress") is not None:
        fail("signoz-ingress must NOT render for prod (ADV-011 dev-tooling exception)")

    print(
        "OK: OBS-P1 contract holds — signoz-otel-collector ingest (images consistent with "
        "the values pins), memory_limiter everywhere, k8sattributes on traces/metrics only, "
        "single sequential schema-migrator job + gateway sync-check gate, credentials via "
        "secretKeyRef, agent (health_check probes) forwards to the gateway, adoption "
        "invariants intact, ingress Pre-Dev-only with explicit namespace"
    )


if __name__ == "__main__":
    main()
