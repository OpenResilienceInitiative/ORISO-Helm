#!/usr/bin/env python3
"""OBS-P4a: render-based contract for the otel-agent's metrics receivers.

Locks the invariants that make the imported "Kubernetes Pod Metrics -
Overall" and "Kubernetes Node Metrics - Overall" SigNoz community dashboards
(https://github.com/SigNoz/dashboards, k8s-infra-metrics/) actually show
data on Pre-Dev, without re-breaking the OBS-P1 log-collection contract:

  - the otel-agent DaemonSet gains hostmetrics + kubeletstats receivers in a
    dedicated `metrics` pipeline, alongside (not instead of) the existing
    `logs` pipeline built on filelog — both pipelines start with
    memory_limiter (K8-L02),
  - kubeletstats is the receiver that actually feeds the two dashboards:
    every widget in both dashboard JSONs aggregates a k8s.pod.* or
    k8s.node.* metric, which is kubeletstats' own metric namespace,
  - the agent's metrics pipeline exports OTLP to the SAME gateway service
    the logs pipeline already uses (no second ingest path, no duplicated
    ClickHouse pipeline — the gateway's existing `metrics` pipeline in
    otel-collector-configmap.yaml already writes to
    signozclickhousemetrics/signozmeter),
  - kubeletstats authenticates with the ServiceAccount token and RBAC grants
    exactly `nodes/stats:get` (least privilege — that is the only subresource
    the kubelet's SubjectAccessReview checks for this receiver; no broader
    nodes/pods list-watch is needed),
  - the DaemonSet runs under that dedicated ServiceAccount, not `default`.
"""

from __future__ import annotations

import os
import subprocess
import sys

import yaml


CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = "oriso"
SIGNOZ_TEST_SET = [
    "signoz.enabled=true",
    "signoz.otelAgent.enabled=true",
    "signoz.otelAgent.logsNamespace=caritas",
]


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def render() -> list[dict]:
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
    for setting in SIGNOZ_TEST_SET:
        command += ["--set", setting]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"chart render failed:\n{result.stderr}")
    return [document for document in yaml.safe_load_all(result.stdout) if isinstance(document, dict)]


def resource(documents: list[dict], kind: str, name: str) -> dict:
    for document in documents:
        if document.get("kind") == kind and document.get("metadata", {}).get("name") == name:
            return document
    fail(f"rendered {kind}/{name} is missing")


def agent_config(documents: list[dict]) -> dict:
    configmap = resource(documents, "ConfigMap", f"{RELEASE}-otel-agent")
    return yaml.safe_load(configmap["data"]["otel-agent-config.yaml"])


def main() -> None:
    documents = render()
    config = agent_config(documents)

    # --- receivers: hostmetrics + kubeletstats present --------------------
    receivers = config["receivers"]
    if "hostmetrics" not in receivers:
        fail("agent config is missing the hostmetrics receiver")
    if "kubeletstats" not in receivers:
        fail("agent config is missing the kubeletstats receiver")
    if "filelog" not in receivers:
        fail("agent config lost the existing filelog receiver (OBS-P1 regression)")

    # kubeletstats is what actually feeds the two imported dashboards: every
    # widget's aggregation.metricName in both dashboard JSONs is k8s.pod.* or
    # k8s.node.*, which only kubeletstats' metric_groups produce.
    kubeletstats = receivers["kubeletstats"]
    if kubeletstats.get("auth_type") != "serviceAccount":
        fail(f"kubeletstats must use auth_type serviceAccount, got {kubeletstats.get('auth_type')}")
    groups = kubeletstats.get("metric_groups", [])
    for required in ("node", "pod"):
        if required not in groups:
            fail(f"kubeletstats metric_groups must include '{required}' (dashboard widgets query k8s.{required}.*), got {groups}")
    if "10250" not in kubeletstats.get("endpoint", ""):
        fail(f"kubeletstats must target the kubelet API port 10250, got {kubeletstats.get('endpoint')}")

    # --- pipelines: metrics alongside logs, memory_limiter first ----------
    pipelines = config["service"]["pipelines"]
    if "logs" not in pipelines:
        fail("agent lost its logs pipeline (OBS-P1 regression)")
    if "metrics" not in pipelines:
        fail("agent is missing a metrics pipeline")

    for signal in ("logs", "metrics"):
        processors = pipelines[signal].get("processors", [])
        if not processors or processors[0] != "memory_limiter":
            fail(f"agent {signal} pipeline must start with memory_limiter (K8-L02), got {processors}")

    metrics_receivers = pipelines["metrics"].get("receivers", [])
    if "hostmetrics" not in metrics_receivers or "kubeletstats" not in metrics_receivers:
        fail(f"agent metrics pipeline must run both hostmetrics and kubeletstats, got {metrics_receivers}")
    if pipelines["logs"]["receivers"] != ["filelog"]:
        fail(f"agent logs pipeline must stay filelog-only (OBS-P1 regression), got {pipelines['logs']['receivers']}")

    # --- export: metrics reuse the SAME otlp exporter/endpoint as logs -----
    # No second ingest path: the gateway's existing `metrics` pipeline
    # (otel-collector-configmap.yaml) already writes to
    # signozclickhousemetrics/signozmeter.
    gateway_service = f"{RELEASE}-otel-collector"
    logs_exporters = pipelines["logs"].get("exporters", [])
    metrics_exporters = pipelines["metrics"].get("exporters", [])
    if logs_exporters != metrics_exporters:
        fail(
            f"agent metrics pipeline must reuse the same exporter as logs (no duplicated ingest "
            f"path), got logs={logs_exporters} metrics={metrics_exporters}"
        )
    agent_endpoint = config["exporters"]["otlp"]["endpoint"]
    if gateway_service not in agent_endpoint or not agent_endpoint.endswith(":4317"):
        fail(f"agent metrics export endpoint must match the gateway service on :4317, got {agent_endpoint}")

    # --- RBAC: nodes/stats:get, least privilege ----------------------------
    role = resource(documents, "ClusterRole", f"{RELEASE}-otel-agent")
    rules = role.get("rules", [])
    stats_rule = next(
        (r for r in rules if "nodes/stats" in r.get("resources", [])),
        None,
    )
    if stats_rule is None:
        fail(f"otel-agent ClusterRole is missing a nodes/stats rule, got {rules}")
    if "get" not in stats_rule.get("verbs", []):
        fail(f"nodes/stats rule must grant 'get', got {stats_rule.get('verbs')}")
    # Least privilege: no broader core-API list/watch grants riding along.
    for rule in rules:
        if rule is stats_rule:
            continue
        fail(f"otel-agent ClusterRole carries an extra rule beyond nodes/stats: {rule}")

    binding = resource(documents, "ClusterRoleBinding", f"{RELEASE}-otel-agent")
    if binding["roleRef"]["name"] != f"{RELEASE}-otel-agent":
        fail("otel-agent ClusterRoleBinding must bind the otel-agent ClusterRole")
    subject = binding["subjects"][0]
    if subject["kind"] != "ServiceAccount" or subject["name"] != f"{RELEASE}-otel-agent":
        fail(f"otel-agent ClusterRoleBinding must target the otel-agent ServiceAccount, got {subject}")

    resource(documents, "ServiceAccount", f"{RELEASE}-otel-agent")

    # --- DaemonSet runs under that ServiceAccount, mounts /hostfs ----------
    agent = resource(documents, "DaemonSet", f"{RELEASE}-otel-agent")
    pod_spec = agent["spec"]["template"]["spec"]
    if pod_spec.get("serviceAccountName") != f"{RELEASE}-otel-agent":
        fail(f"otel-agent DaemonSet must run under its dedicated ServiceAccount, got {pod_spec.get('serviceAccountName')}")

    container = pod_spec["containers"][0]
    env = {e["name"]: e for e in container.get("env", [])}
    if "K8S_NODE_IP" not in env or "fieldRef" not in env["K8S_NODE_IP"].get("valueFrom", {}):
        fail("otel-agent container must expose K8S_NODE_IP via the downward API for kubeletstats")

    mounts = {m["mountPath"]: m for m in container.get("volumeMounts", [])}
    if "/hostfs" not in mounts or not mounts["/hostfs"].get("readOnly"):
        fail("otel-agent must mount /hostfs read-only for the hostmetrics receiver")
    if "/var/log/pods" not in mounts or not mounts["/var/log/pods"].get("readOnly"):
        fail("otel-agent must keep mounting /var/log/pods read-only (OBS-P1 regression)")

    print(
        "OK: OBS-P4a contract holds — hostmetrics + kubeletstats receivers alongside the "
        "existing filelog receiver, metrics pipeline starts with memory_limiter and reuses the "
        "logs pipeline's otlp exporter into the gateway (no duplicated ingest path), RBAC scoped "
        "to nodes/stats:get only, DaemonSet runs under its own ServiceAccount with /hostfs mounted"
    )


if __name__ == "__main__":
    main()
