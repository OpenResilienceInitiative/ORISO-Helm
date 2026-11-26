#!/usr/bin/env python3
"""Prove ORISO SigNoz persists OTLP and Kubernetes infrastructure signals.

The default mode is an active, privacy-safe acceptance check: it emits one
synthetic trace, metric and correlated log through the in-cluster OTLP/HTTP
receiver, exercises infrastructure collection, then reads the signals back from
ClickHouse. No credentials or real application payloads are printed or stored.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

SERVICE_NAME = "oriso-signoz-acceptance"
METRIC_NAME = "oriso.signoz.acceptance"
SUPPRESSED_LOG_BODY = "[ORISO log body suppressed by privacy policy]"
ARGOCD_MIGRATION_JOB = "signoz-telemetrystore-migrator-sync"
# Match the managed ClickHouse capacity alert's 2 GiB floor.
MIN_CLICKHOUSE_UNRESERVED_BYTES = 2 * 1024**3
CLICKHOUSE_UNKNOWN_SPACE = 2**64 - 1
CLICKHOUSE_LOG_TABLES = (
    "query_log", "text_log", "trace_log", "metric_log", "asynchronous_metric_log",
    "part_log", "zookeeper_log", "aggregated_zookeeper_log", "processors_profile_log",
)
# shortcut: Dev also removes zookeeper_log (#519) but the existing table keeps its 3-day TTL, so the
# check treats it as enabled; a fresh ClickHouse volume reports it missing. Add it here if that happens.
CLICKHOUSE_REMOVABLE_WRITERS = ("metric_log", "trace_log")
CLICKHOUSE_LOG_RETENTION_DAYS = 3
MAX_CLICKHOUSE_DISK_USED_PERCENT = 75
MAX_CLICKHOUSE_LOG_BYTES = 10 * 1024**3
JAVA_SERVICES = ("user-service", "agency-service", "tenant-service", "consulting-type-service")
JAVA_SIGNALS = ("logs", "metrics", "traces", "logToTrace")
JAVA_FRESHNESS_MINUTES = 15
JAVA_CORRELATION_MINUTES = 60
CLICKHOUSE_EXPORTERS = {
    "traces": "clickhousetraces",
    "metrics": "signozclickhousemetrics",
    "logs": "clickhouselogsexporter",
}


def _attribute(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def build_otlp_payloads(
    acceptance_id: str,
    environment: str,
    timestamp_ns: int,
    trace_id: str,
    span_id: str,
) -> dict[str, dict[str, Any]]:
    """Build correlated OTLP/HTTP JSON payloads containing synthetic data only."""
    resource = {
        "attributes": [
            _attribute("service.name", SERVICE_NAME),
            _attribute("deployment.environment", environment),
            _attribute("oriso.acceptance.id", acceptance_id),
        ]
    }
    end_ns = timestamp_ns + 1_000_000
    return {
        "traces": {
            "resourceSpans": [
                {
                    "resource": resource,
                    "scopeSpans": [
                        {
                            "scope": {"name": "oriso.signoz.acceptance"},
                            "spans": [
                                {
                                    "traceId": trace_id,
                                    "spanId": span_id,
                                    "name": "signoz.runtime.acceptance",
                                    "kind": 2,
                                    "startTimeUnixNano": str(timestamp_ns),
                                    "endTimeUnixNano": str(end_ns),
                                    "attributes": [
                                        _attribute("oriso.acceptance.id", acceptance_id)
                                    ],
                                    "status": {"code": 1},
                                }
                            ],
                        }
                    ],
                }
            ]
        },
        "metrics": {
            "resourceMetrics": [
                {
                    "resource": resource,
                    "scopeMetrics": [
                        {
                            "scope": {"name": "oriso.signoz.acceptance"},
                            "metrics": [
                                {
                                    "name": METRIC_NAME,
                                    "description": "ORISO SigNoz acceptance canary",
                                    "unit": "1",
                                    "gauge": {
                                        "dataPoints": [
                                            {
                                                "timeUnixNano": str(timestamp_ns),
                                                "asDouble": 1,
                                                "attributes": [
                                                    _attribute(
                                                        "oriso.acceptance.id",
                                                        acceptance_id,
                                                    )
                                                ],
                                            }
                                        ]
                                    },
                                }
                            ],
                        }
                    ],
                }
            ]
        },
        "logs": {
            "resourceLogs": [
                {
                    "resource": resource,
                    "scopeLogs": [
                        {
                            "scope": {"name": "oriso.signoz.acceptance"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(timestamp_ns),
                                    "observedTimeUnixNano": str(timestamp_ns),
                                    "severityNumber": 9,
                                    "severityText": "INFO",
                                    "body": {
                                        "stringValue": "SigNoz runtime acceptance canary"
                                    },
                                    "attributes": [
                                        _attribute("oriso.acceptance.id", acceptance_id)
                                    ],
                                    "traceId": trace_id,
                                    "spanId": span_id,
                                }
                            ],
                        }
                    ],
                }
            ]
        },
    }


def build_application_log_line(
    acceptance_id: str,
    trace_id: str,
    span_id: str,
    forbidden_marker: str,
) -> str:
    """Build a representative ORISO JSON log whose free text must be removed."""
    return json.dumps(
        {
            "serviceName": "oriso-signoz-log-acceptance",
            "traceId": trace_id,
            "spanId": span_id,
            "orisoAcceptanceId": acceptance_id,
            "log": {
                "level": "INFO",
                "logger": "SigNozAcceptance",
                "message": f"privacy probe {forbidden_marker}",
                "stack": f"synthetic stack {forbidden_marker}",
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _sql_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _metric_samples_query(*series_predicates: str) -> str:
    """Count recent metric samples whose series has the required identity."""
    predicate_sql = "\n              AND ".join(series_predicates)
    return f"""
        SELECT count() FROM signoz_metrics.samples_v4
        WHERE unix_milli > toUnixTimestamp64Milli(now64() - INTERVAL 15 MINUTE)
          AND (env, temporality, metric_name, fingerprint) IN
          (
            SELECT env, temporality, metric_name, fingerprint
            FROM signoz_metrics.time_series_v4
            WHERE {predicate_sql}
          )
    """.strip()


def _java_signal_starts(end_ms: int, since_ms: int | None) -> tuple[int, int]:
    if since_ms is not None and not 0 <= since_ms < end_ms:
        raise RuntimeError("--since-unix-ms must be a nonnegative timestamp before now")
    fresh_ms = max(end_ms - JAVA_FRESHNESS_MINUTES * 60_000, since_ms or 0)
    correlation_ms = max(end_ms - JAVA_CORRELATION_MINUTES * 60_000, since_ms or 0)
    return fresh_ms, correlation_ms


def build_java_signal_query(environment: str, end_ms: int, since_ms: int | None = None) -> str:
    """Count real stored signals and log-to-span matches without exporting identities."""
    env = _sql_literal(environment)
    services = ", ".join(_sql_literal(service) for service in JAVA_SERVICES)
    fresh_ms, correlation_ms = _java_signal_starts(end_ms, since_ms)
    # The tuple predicate prunes the sample sorting key before the join attributes rows to services.
    return f"""WITH recent_logs AS (
    SELECT resources_string['service.name'] AS service_name, timestamp, trace_id, span_id
    FROM signoz_logs.distributed_logs_v2
    WHERE timestamp >= {correlation_ms * 1_000_000} AND timestamp < {end_ms * 1_000_000}
      AND resources_string['deployment.environment'] = {env} AND service_name IN ({services})
), recent_traces AS (
    SELECT t.serviceName AS service_name, toString(t.trace_id) AS trace_id, t.span_id,
        t.timestamp >= fromUnixTimestamp64Milli({fresh_ms}) AS is_fresh,
        t.resources_string['deployment.environment'] AS deployment_environment,
        lower(trimBoth(t.resources_string['service.version'], ' \\t\\n\\v\\f\\r')) AS normalized_version
    FROM signoz_traces.distributed_signoz_index_v3 AS t
    WHERE t.timestamp >= fromUnixTimestamp64Milli({correlation_ms})
      AND t.timestamp < fromUnixTimestamp64Milli({end_ms})
      AND t.ts_bucket_start BETWEEN {correlation_ms // 1000 - 1800} AND {end_ms // 1000}
      AND t.serviceName IN ({services})
), metric_identities AS (
    SELECT DISTINCT env, temporality, metric_name, fingerprint,
        resource_attrs['service.name'] AS service_name
    FROM signoz_metrics.distributed_time_series_v4
    WHERE unix_milli >= {fresh_ms - 3_600_000} AND unix_milli < {end_ms}
      AND resource_attrs['deployment.environment'] = {env} AND service_name IN ({services})
)
SELECT signal, service_name, samples, wrong_environment, invalid_version FROM (
    SELECT 'logs' AS signal, service_name, countIf(timestamp >= {fresh_ms * 1_000_000}) AS samples,
        0 AS wrong_environment, 0 AS invalid_version
    FROM recent_logs GROUP BY service_name
    UNION ALL
    SELECT 'traces' AS signal, service_name,
        countIf(is_fresh) AS samples,
        countIf(is_fresh AND deployment_environment != {env}) AS wrong_environment,
        countIf(is_fresh AND normalized_version IN ('', 'unknown')) AS invalid_version
    FROM recent_traces GROUP BY service_name
    UNION ALL
    SELECT 'metrics' AS signal, i.service_name AS service_name, count() AS samples,
        0 AS wrong_environment, 0 AS invalid_version
    FROM signoz_metrics.distributed_samples_v4 AS s
    GLOBAL INNER JOIN metric_identities AS i USING (env, temporality, metric_name, fingerprint)
    WHERE s.unix_milli >= {fresh_ms} AND s.unix_milli < {end_ms}
      AND (s.env, s.temporality, s.metric_name, s.fingerprint) GLOBAL IN (
        SELECT env, temporality, metric_name, fingerprint FROM metric_identities
      )
      AND bitAnd(s.flags, 1) = 0 AND isFinite(s.value)
    GROUP BY i.service_name
    UNION ALL
    SELECT 'logToTrace' AS signal, service_name, count() AS samples,
        0 AS wrong_environment, 0 AS invalid_version
    FROM recent_logs
    WHERE length(trace_id) = 32 AND trace_id != '00000000000000000000000000000000'
      AND length(span_id) = 16 AND span_id != '0000000000000000'
      AND (service_name, trace_id, span_id) GLOBAL IN (
        SELECT DISTINCT service_name, trace_id, span_id FROM recent_traces
        WHERE deployment_environment = {env}
      )
    GROUP BY service_name
)
ORDER BY signal, service_name
SETTINGS max_execution_time = 8, max_rows_to_read = 2000000, max_bytes_to_read = 134217728,
  timeout_overflow_mode = 'throw', read_overflow_mode = 'throw', log_queries = 0"""


def build_readback_queries(
    environment: str,
    acceptance_id: str,
    service_name: str = SERVICE_NAME,
    metric_name: str = METRIC_NAME,
) -> dict[str, str]:
    """Return current SigNoz schema queries for synthetic-signal readback."""
    service = _sql_literal(service_name)
    env = _sql_literal(environment)
    acceptance = _sql_literal(acceptance_id)
    metric = _sql_literal(metric_name)
    return {
        "traces": f"""
            SELECT count() FROM signoz_traces.signoz_index_v3
            WHERE timestamp > now() - INTERVAL 15 MINUTE
              AND resources_string['service.name'] = {service}
              AND resources_string['deployment.environment'] = {env}
              AND resources_string['oriso.acceptance.id'] = {acceptance}
        """.strip(),
        "metrics": _metric_samples_query(
            f"metric_name = {metric}",
            f"resource_attrs['service.name'] = {service}",
            f"resource_attrs['deployment.environment'] = {env}",
            f"resource_attrs['oriso.acceptance.id'] = {acceptance}",
        ),
        "logs": f"""
            SELECT count() FROM signoz_logs.logs_v2
            WHERE timestamp > toUnixTimestamp(now() - INTERVAL 15 MINUTE) * 1000000000
              AND resources_string['service.name'] = {service}
              AND resources_string['deployment.environment'] = {env}
              AND resources_string['oriso.acceptance.id'] = {acceptance}
        """.strip(),
    }


def build_infra_readback_queries(
    environment: str,
    cluster_name: str,
    acceptance_id: str,
    trace_id: str,
    forbidden_marker: str,
) -> dict[str, str]:
    """Return queries proving Kubernetes signal breadth and log privacy."""
    env = _sql_literal(environment)
    cluster = _sql_literal(cluster_name)
    acceptance = _sql_literal(acceptance_id)
    trace = _sql_literal(trace_id)
    forbidden = _sql_literal(forbidden_marker)
    suppressed = _sql_literal(SUPPRESSED_LOG_BODY)
    metric_identity = (
        f"resource_attrs['deployment.environment'] = {env}",
        f"resource_attrs['k8s.cluster.name'] = {cluster}",
    )
    log_scope = f"""
              AND resources_string['deployment.environment'] = {env}
              AND resources_string['k8s.cluster.name'] = {cluster}
    """.rstrip()
    return {
        "podMetrics": _metric_samples_query(
            "startsWith(metric_name, 'k8s.pod.')", *metric_identity
        ),
        "nodeMetrics": _metric_samples_query(
            "startsWith(metric_name, 'k8s.node.')", *metric_identity
        ),
        "nodeFilesystemAvailable": _metric_samples_query(
            "metric_name = 'k8s.node.filesystem.available'", *metric_identity
        ),
        "nodeFilesystemCapacity": _metric_samples_query(
            "metric_name = 'k8s.node.filesystem.capacity'", *metric_identity
        ),
        "hostMetrics": _metric_samples_query(
            "startsWith(metric_name, 'system.')", *metric_identity
        ),
        "nodeCondition": _metric_samples_query(
            "metric_name = 'k8s.node.condition'", *metric_identity
        ),
        "collectorSelfMetrics": _metric_samples_query(
            "startsWith(metric_name, 'otelcol_')",
            "startsWith(resource_attrs['service.name'], 'oriso-k8s-infra')",
            *metric_identity,
        ),
        "kubernetesEvent": f"""
            SELECT count() FROM signoz_logs.logs_v2
            WHERE timestamp > toUnixTimestamp(now() - INTERVAL 15 MINUTE) * 1000000000
              AND position(body, {acceptance}) > 0
{log_scope}
        """.strip(),
        "privacySafeApplicationLog": f"""
            SELECT count() FROM signoz_logs.logs_v2
            WHERE timestamp > toUnixTimestamp(now() - INTERVAL 15 MINUTE) * 1000000000
              AND trace_id = {trace}
              AND body = {suppressed}
              AND attributes_string['service.name'] = 'oriso-signoz-log-acceptance'
{log_scope}
        """.strip(),
        "forbiddenLogBody": f"""
            SELECT count() FROM signoz_logs.logs_v2
            WHERE timestamp > toUnixTimestamp(now() - INTERVAL 15 MINUTE) * 1000000000
              AND trace_id = {trace}
              AND positionCaseInsensitive(body, {forbidden}) > 0
{log_scope}
        """.strip(),
    }


def infra_readback_failures(counts: dict[str, int]) -> list[str]:
    """Explain missing infrastructure signals or a failed privacy assertion."""
    failures = [
        f"{signal} missing"
        for signal, count in counts.items()
        if signal != "forbiddenLogBody" and count < 1
    ]
    if counts.get("forbiddenLogBody", 0) != 0:
        failures.append("forbiddenLogBody leaked")
    return failures


def infra_rollout_targets(release: str) -> tuple[str, str]:
    """Return the node-local and cluster-wide collector workloads."""
    return (
        f"daemonset/{release}-k8s-infra-otel-agent",
        f"deployment/{release}-k8s-infra-otel-deployment",
    )


def validate_collector_config(config: dict[str, Any]) -> None:
    """Reject a collector that is alive but has an incomplete OTLP pipeline."""
    protocols = config.get("receivers", {}).get("otlp", {}).get("protocols", {})
    missing_protocols = {"grpc", "http"} - set(protocols)
    if missing_protocols:
        raise RuntimeError(
            "collector OTLP receiver lacks protocols: "
            + ", ".join(sorted(missing_protocols))
        )

    pipelines = config.get("service", {}).get("pipelines", {})
    exporters = set(config.get("exporters", {}))
    for signal, required_exporter in CLICKHOUSE_EXPORTERS.items():
        pipeline = pipelines.get(signal, {})
        if "otlp" not in pipeline.get("receivers", []):
            raise RuntimeError(f"{signal} pipeline lacks the OTLP receiver")
        if required_exporter not in exporters or required_exporter not in pipeline.get(
            "exporters", []
        ):
            raise RuntimeError(f"{signal} pipeline lacks its ClickHouse exporter")


class Runner:
    """Run kubectl locally or through a batch-mode SSH connection."""

    def __init__(self, ssh_host: str | None = None) -> None:
        self.ssh_host = ssh_host

    def run(
        self, command: list[str], input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        effective_command = command
        if self.ssh_host:
            effective_command = [
                "ssh",
                "-o",
                "BatchMode=yes",
                self.ssh_host,
                shlex.join(command),
            ]
        return subprocess.run(
            effective_command,
            input=input_text,
            text=True,
            capture_output=True,
            check=True,
        )


def _kubectl(runner: Runner, namespace: str, *arguments: str) -> str:
    return runner.run(["kubectl", "-n", namespace, *arguments]).stdout.strip()


def _check_runtime_readiness(
    runner: Runner,
    namespace: str,
    release: str,
    timeout: str,
    require_k8s_infra: bool = False,
    *,
    argocd_application: str | None = None,
    argocd_migration_job: str = ARGOCD_MIGRATION_JOB,
) -> dict[str, Any] | None:
    statefulsets = _current_clickhouse_statefulsets(runner, namespace, release)

    _kubectl(
        runner, namespace, "rollout", "status", f"deployment/{release}-clickhouse-operator",
        f"--timeout={timeout}",
    )
    rollout_targets = (
        *(f"statefulset/{statefulset['metadata']['name']}" for statefulset in statefulsets),
        f"deployment/{release}-signoz-otel-collector",
        f"statefulset/{release}-signoz",
    )
    for target in rollout_targets:
        _kubectl(runner, namespace, "rollout", "status", target, f"--timeout={timeout}")

    if require_k8s_infra:
        for target in infra_rollout_targets(release):
            _kubectl(
                runner,
                namespace,
                "rollout",
                "status",
                target,
                f"--timeout={timeout}",
            )

    if argocd_application is not None:
        return _check_argocd_migration(runner, namespace, argocd_application, argocd_migration_job)

    _kubectl(
        runner,
        namespace,
        "wait",
        "--for=condition=complete",
        f"job/{_current_migration_job(runner, namespace, release)}",
        f"--timeout={timeout}",
    )
    return None


def _check_argocd_migration(
    runner: Runner, namespace: str, application_ref: str, job_name: str,
) -> dict[str, Any]:
    app_namespace, app_name = application_ref.split("/")
    application = json.loads(_kubectl(
        runner, app_namespace, "get", "applications.argoproj.io", app_name, "-o", "json",
    ))
    status = application.get("status", {})
    comparison = status.get("sync", {})
    compared_to = comparison.get("comparedTo", {})
    state = status.get("operationState", {})
    operation = state.get("operation", {}).get("sync")
    result = state.get("syncResult", {})
    spec = application.get("spec", {})
    destination = spec.get("destination", {})
    if (
        destination.get("namespace") != namespace
        or destination.get("server") != "https://kubernetes.default.svc"
        or compared_to.get("destination") != destination
    ):
        raise RuntimeError(
            f"Argo CD Application {application_ref} must target the local cluster and namespace {namespace}"
        )
    if (
        application.get("metadata", {}).get("deletionTimestamp")
        or application.get("operation")
        or comparison.get("status") != "Synced"
        or state.get("phase") != "Succeeded"
        or operation is None
        or any(operation.get(key) for key in ("dryRun", "resources", "manifests"))
    ):
        raise RuntimeError(f"Argo CD Application {application_ref} has no current successful full sync")

    # Check the applied source configuration as well as its resolved revision.
    # An updated Helm source can change values at the same Git or chart revision.
    if spec.get("sources"):
        sources = spec["sources"]
        revisions = comparison.get("revisions", [])
        current = (
            len(revisions) == len(sources) and all(revisions)
            and result.get("revisions") == revisions
            and compared_to.get("sources") == result.get("sources") == sources
        )
    else:
        source = spec.get("source")
        revision = comparison.get("revision")
        revisions = [revision]
        current = (
            source and revision and result.get("revision") == revision
            and compared_to.get("source") == result.get("source") == source
        )
    if not current:
        raise RuntimeError(f"Argo CD Application {application_ref} has no successful sync for its current sources and revisions")

    identity = {
        "group": "batch",
        "version": "v1",
        "kind": "Job",
        "namespace": namespace,
        "name": job_name,
        "hookType": "Sync",
        "syncPhase": "Sync",
    }
    matching_hooks = [
        resource for resource in result.get("resources", [])
        if all(resource.get(key) == value for key, value in identity.items())
    ]
    if len(matching_hooks) != 1 or matching_hooks[0].get("hookPhase") != "Succeeded":
        raise RuntimeError(f"Argo CD Application {application_ref} has no successful Sync hook for Job {namespace}/{job_name}")
    return {"application": application_ref, "job": job_name, "revisions": revisions}


def _current_migration_job(runner: Runner, namespace: str, release: str) -> str:
    mode_label = "oriso.org/signoz-migrator"
    selector = f"app.kubernetes.io/instance={release},{mode_label}"
    jobs = json.loads(_kubectl(runner, namespace, "get", "job", "-l", selector, "-o", "json"))
    by_mode: dict[str, list[str]] = {"managed": [], "hook": []}
    for item in jobs["items"]:
        metadata = item["metadata"]
        mode = metadata["labels"][mode_label]
        if not metadata.get("deletionTimestamp") and mode in by_mode:
            by_mode[mode].append(metadata["name"])

    names = by_mode["managed"] or by_mode["hook"]
    if len(names) > 1:
        raise RuntimeError(f"Multiple SigNoz migration Jobs for {release}: {', '.join(sorted(names))}")
    return names[0] if names else "signoz-telemetrystore-migrator"


def _current_clickhouse_statefulsets(
    runner: Runner, namespace: str, release: str, *, chi: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    chi_name = f"{release}-clickhouse"
    if chi is None:
        chi = json.loads(_kubectl(runner, namespace, "get", "chi", chi_name, "-o", "json"))
    installation = chi.get("metadata", {})
    if installation.get("deletionTimestamp"):
        raise RuntimeError(f"ClickHouseInstallation {chi_name} is deleting")
    installation_uid = installation.get("uid")
    if not installation_uid:
        raise RuntimeError(f"ClickHouseInstallation {chi_name} has no UID")

    statefulsets = json.loads(_kubectl(
        runner, namespace, "get", "statefulset", "-l", f"clickhouse.altinity.com/chi={chi_name}",
        "-o", "json",
    ))["items"]
    owned_statefulsets = []
    for statefulset in statefulsets:
        metadata = statefulset["metadata"]
        if not any(
            owner.get("controller") is True
            and owner.get("kind") == "ClickHouseInstallation"
            and owner.get("name") == chi_name
            and owner.get("uid") == installation_uid
            for owner in metadata.get("ownerReferences", [])
        ):
            continue
        name = metadata["name"]
        if metadata.get("deletionTimestamp"):
            raise RuntimeError(f"ClickHouse StatefulSet {name} is deleting")
        if statefulset["spec"].get("replicas", 1) < 1:
            raise RuntimeError(f"ClickHouse StatefulSet {name} is scaled to zero")
        if not metadata.get("uid"):
            raise RuntimeError(f"ClickHouse StatefulSet {name} has no UID")
        owned_statefulsets.append(statefulset)
    if not owned_statefulsets:
        raise RuntimeError(f"ClickHouseInstallation {chi_name} has no owned StatefulSets")
    return sorted(owned_statefulsets, key=lambda statefulset: statefulset["metadata"]["name"])


def _check_collector_config(runner: Runner, namespace: str, release: str) -> None:
    raw = _kubectl(
        runner,
        namespace,
        "get",
        "configmap",
        f"{release}-signoz-otel-collector",
        "-o",
        "json",
    )
    config_map = json.loads(raw)
    config_text = config_map.get("data", {}).get("otel-collector-config.yaml")
    if not config_text:
        raise RuntimeError("collector ConfigMap lacks otel-collector-config.yaml")
    validate_collector_config(yaml.safe_load(config_text) or {})


def _emit_signal(
    runner: Runner,
    namespace: str,
    release: str,
    acceptance_id: str,
    signal: str,
    payload: dict[str, Any],
) -> None:
    pod_name = f"signoz-acceptance-{acceptance_id[-8:]}-{signal}"
    endpoint = f"http://{release}-signoz-otel-collector:4318/v1/{signal}"
    runner.run(
        [
            "kubectl",
            "-n",
            namespace,
            "run",
            pod_name,
            "--quiet",
            "--rm",
            "--restart=Never",
            "--attach",
            "--stdin",
            "--image=curlimages/curl:8.12.1",
            "--",
            "-fsS",
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            "@-",
            endpoint,
        ],
        input_text=json.dumps(payload),
    )


def build_acceptance_event(namespace: str, acceptance_id: str) -> dict[str, Any]:
    """Core v1 Event whose message carries only the random acceptance ID."""
    # k8s_events skips events older than its start time, so stamp the current time.
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "apiVersion": "v1",
        "kind": "Event",
        "metadata": {"name": f"signoz-acceptance-{acceptance_id[-8:]}", "namespace": namespace},
        "involvedObject": {"apiVersion": "v1", "kind": "Namespace", "name": namespace, "namespace": namespace},
        "reason": "SigNozAcceptance",
        "type": "Normal",
        "message": acceptance_id,
        "source": {"component": "oriso-signoz-acceptance"},
        "firstTimestamp": now,
        "lastTimestamp": now,
        "count": 1,
    }


def _emit_infra_probes(
    runner: Runner,
    namespace: str,
    acceptance_id: str,
    trace_id: str,
    span_id: str,
    forbidden_marker: str,
) -> str:
    """Create one Kubernetes event and one representative application log."""
    suffix = acceptance_id[-8:]
    # kubectl has no "create event" subcommand, so submit a core Event manifest.
    runner.run(
        ["kubectl", "-n", namespace, "create", "-f", "-"],
        input_text=json.dumps(build_acceptance_event(namespace, acceptance_id)),
    )

    pod_name = f"signoz-log-acceptance-{suffix}"
    log_line = build_application_log_line(
        acceptance_id,
        trace_id,
        span_id,
        forbidden_marker,
    )
    try:
        runner.run(
            [
                "kubectl",
                "-n",
                namespace,
                "run",
                pod_name,
                "--quiet",
                "--restart=Never",
                "--image=busybox:1.36.1",
                "--command",
                "--",
                "sh",
                "-c",
                "sleep 5; printf '%s\\n' \"$1\"; sleep 10",
                "signoz-log-probe",
                log_line,
            ]
        )
        _kubectl(
            runner,
            namespace,
            "wait",
            "--for=jsonpath={.status.phase}=Succeeded",
            f"pod/{pod_name}",
            "--timeout=45s",
        )
    except subprocess.CalledProcessError:
        _delete_probe_pod(runner, namespace, pod_name)
        raise
    return pod_name


def _delete_probe_pod(runner: Runner, namespace: str, pod_name: str) -> None:
    runner.run(
        [
            "kubectl",
            "-n",
            namespace,
            "delete",
            "pod",
            pod_name,
            "--ignore-not-found",
            "--wait=false",
        ]
    )


def _clickhouse_pods(runner: Runner, namespace: str, release: str) -> list[str]:
    statefulset_uids = {
        statefulset["metadata"]["uid"]
        for statefulset in _current_clickhouse_statefulsets(runner, namespace, release)
    }
    output = _kubectl(
        runner,
        namespace,
        "get",
        "pods",
        "-l",
        f"clickhouse.altinity.com/chi={release}-clickhouse,"
        "app.kubernetes.io/component=clickhouse",
        "-o",
        "json",
    )
    pods = []
    for item in json.loads(output)["items"]:
        metadata = item["metadata"]
        if metadata.get("deletionTimestamp"):
            continue
        if not any(
            owner.get("controller") is True
            and owner.get("kind") == "StatefulSet"
            and owner.get("uid") in statefulset_uids
            for owner in metadata.get("ownerReferences", [])
        ):
            continue
        name = metadata["name"]
        if not any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in item.get("status", {}).get("conditions", [])
        ):
            raise RuntimeError(f"ClickHouse pod {name} is not ready")
        pods.append(name)
    if not pods:
        raise RuntimeError("ClickHouse data pods were not found")
    return sorted(pods)


def _clickhouse_query(
    runner: Runner, namespace: str, pod: str, query: str, output_format: str, *, read_only: bool = False,
) -> str:
    options = (
        ("--host", "127.0.0.1", "--readonly=1", "--log_queries=0", "--max_execution_time=5")
        if read_only else ()
    )
    return _kubectl(
        runner,
        namespace,
        "exec",
        pod,
        "-c",
        "clickhouse",
        "--",
        "clickhouse-client",
        *options,
        "--format",
        output_format,
        "--query",
        query,
    )


def _query_integer(runner: Runner, namespace: str, pod: str, query: str) -> int:
    return int(_clickhouse_query(runner, namespace, pod, query, "TabSeparatedRaw"))


def _parse_sample_count(value: Any) -> int:
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if type(value) is not int or value < 0:
        raise ValueError
    return value


def _read_java_signals(
    runner: Runner, namespace: str, pod: str, environment: str, since_ms: int | None = None,
) -> dict[str, Any]:
    end_ms = time.time_ns() // 1_000_000
    output = _clickhouse_query(
        runner, namespace, pod, build_java_signal_query(environment, end_ms, since_ms), "JSONEachRow"
    )
    try:
        return _java_signal_report(
            (json.loads(line) for line in output.splitlines()), environment, end_ms, since_ms,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("ClickHouse returned invalid Java signal readback") from error


def _read_java_signals_api(
    api_url: str, environment: str, since_ms: int | None,
) -> dict[str, Any]:
    try:
        endpoint = urlsplit(api_url)
        if (
            endpoint.scheme != "https" or not endpoint.hostname or endpoint.username is not None
            or endpoint.query or endpoint.fragment or any(char.isspace() for char in api_url)
        ):
            raise ValueError
    except ValueError:
        raise RuntimeError("--signoz-url requires a trusted HTTPS base URL") from None
    api_key = os.environ.get("SIGNOZ_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("SIGNOZ_API_KEY is required with --signoz-url")

    # This script also runs directly from scripts/, outside the repository import path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from files.signoz.signoz_observability import SignozClient, build_query_range_payload

    end_ms = time.time_ns() // 1_000_000
    _, start_ms = _java_signal_starts(end_ms, since_ms)
    name = "JavaSignals"
    payload = build_query_range_payload(
        {"name": name, "query": build_java_signal_query(environment, end_ms, since_ms)},
        start_ms=start_ms, end_ms=end_ms, request_type="raw", query_type="clickhouse_sql",
    )
    client = SignozClient(api_url, api_key, read_only=True)
    try:
        response = client.request("POST", "/api/v5/query_range", payload)
        envelope = response["data"]
        inner = envelope["data"]
        result, = inner["results"]
        rows = result["rows"]
        if rows is None:
            rows = []
        if (
            response["status"] != "success" or envelope["type"] != "raw"
            or result["queryName"] != name or result["nextCursor"] != ""
            or any(part.get("warning") or part.get("warnings") for part in (response, envelope, inner, result))
            or not isinstance(rows, list) or len(rows) > len(JAVA_SERVICES) * len(JAVA_SIGNALS)
        ):
            raise ValueError
        fields = {"service_name", "signal", "samples", "wrong_environment", "invalid_version"}
        counts = [row["data"] for row in rows]
        if any(not isinstance(row, dict) or row.keys() != fields for row in counts):
            raise ValueError
        return _java_signal_report(counts, environment, end_ms, since_ms)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise RuntimeError("SigNoz returned invalid Java signal readback") from error


def _java_signal_report(
    rows: Iterable[dict[str, Any]], environment: str, end_ms: int, since_ms: int | None,
) -> dict[str, Any]:
    counts = {service: dict.fromkeys(JAVA_SIGNALS, 0) for service in JAVA_SERVICES}
    trace_identity = {}
    seen = set()
    for row in rows:
        service, signal = row["service_name"], row["signal"]
        samples = _parse_sample_count(row["samples"])
        pair = (service, signal)
        if signal not in JAVA_SIGNALS or pair in seen:
            raise ValueError
        if signal == "traces":
            wrong_environment = _parse_sample_count(row["wrong_environment"])
            invalid_version = _parse_sample_count(row["invalid_version"])
            if wrong_environment > samples or invalid_version > samples:
                raise ValueError
            trace_identity[service] = {
                "wrongEnvironment": wrong_environment,
                "invalidVersion": invalid_version,
            }
        counts[service][signal] = samples
        seen.add(pair)
    fresh_ms, correlation_ms = _java_signal_starts(end_ms, since_ms)
    return {
        "environment": environment,
        "endUnixMs": end_ms,
        "freshnessMinutes": JAVA_FRESHNESS_MINUTES,
        "correlationMinutes": JAVA_CORRELATION_MINUTES,
        "freshnessStartUnixMs": fresh_ms,
        "correlationStartUnixMs": correlation_ms,
        "services": counts,
        "traceIdentity": trace_identity,
        "missing": [
            f"{service}/{signal}" for service, signals in counts.items()
            for signal, samples in signals.items() if samples == 0
        ],
    }


def _require_java_signals(report: dict[str, Any]) -> None:
    failures = []
    if report["missing"]:
        failures.append("Java signal readback missing " + ", ".join(report["missing"]))
    identity_mismatches = [
        f"{service} ({counts['wrongEnvironment']} wrong environment, {counts['invalidVersion']} invalid version)"
        for service, counts in report["traceIdentity"].items()
        if counts["wrongEnvironment"] or counts["invalidVersion"]
    ]
    if identity_mismatches:
        failures.append("Java trace identity mismatch for " + ", ".join(identity_mismatches))
    if failures:
        raise RuntimeError(". ".join(failures))


def _check_clickhouse_capacity(runner: Runner, namespace: str, pod: str) -> None:
    try:
        available = _query_integer(
            runner, namespace, pod,
            "SELECT unreserved_space FROM system.disks WHERE name = 'default'",
        )
    except ValueError as error:
        raise RuntimeError(f"ClickHouse capacity unreadable on pod {pod}") from error
    # ClickHouse uses the maximum UInt64 when it cannot report available space.
    if not 0 <= available < CLICKHOUSE_UNKNOWN_SPACE:
        raise RuntimeError(f"ClickHouse capacity unreadable on pod {pod}")
    if available < MIN_CLICKHOUSE_UNRESERVED_BYTES:
        raise RuntimeError(
            f"ClickHouse pod {pod} has {available} unreserved bytes. "
            f"At least {MIN_CLICKHOUSE_UNRESERVED_BYTES} bytes are required."
        )


def _clickhouse_byte_count(value: Any) -> int:
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is not int or not 0 <= value < CLICKHOUSE_UNKNOWN_SPACE:
        raise ValueError("invalid ClickHouse byte count")
    return value


def _clickhouse_log_ttl_days(engine: str) -> int | None:
    # Ignore keywords inside quotes. ClickHouse formats INTERVAL DAY as toIntervalDay.
    unquoted = re.sub(
        r"'(?:\\.|''|[^'\\])*'|`(?:\\.|``|[^`\\])*`" r'|"(?:\\.|""|[^"\\])*"',
        "''",
        engine,
    )
    definition = re.split(r"\s+SETTINGS\s+", unquoted, maxsplit=1)[0]
    ttl = re.search(
        r"\sTTL\s+event_date\s*\+\s*toIntervalDay\(([0-9]+)\)(?:\s+DELETE)?\s*$",
        definition,
    )
    return int(ttl[1]) if ttl else None


def _clickhouse_writer_snapshot(runner: Runner, namespace: str, pod: str) -> str:
    command = ["kubectl", "-n", namespace, "exec", pod, "-c", "clickhouse", "-i", "--", "sh", "-s"]
    script = Path(__file__).with_name("signoz_clickhouse_writer_snapshot.sh").read_text(encoding="utf-8")
    return runner.run(command, input_text=script).stdout.strip()


def _read_clickhouse_retention(runner: Runner, namespace: str, pod: str) -> dict[str, Any]:
    def rows(query: str) -> list[dict[str, Any]]:
        output = _clickhouse_query(runner, namespace, pod, query, "JSONEachRow", read_only=True)
        return [json.loads(line) for line in output.splitlines()]

    before = _clickhouse_writer_snapshot(runner, namespace, pod)

    names = ", ".join(map(_sql_literal, CLICKHOUSE_LOG_TABLES))
    family = f"^({'|'.join(CLICKHOUSE_LOG_TABLES)})(_[0-9]+)?$"
    try:
        [disk] = rows("SELECT type, total_space, free_space FROM system.disks WHERE name = 'default'")
        total, free = map(_clickhouse_byte_count, (disk["total_space"], disk["free_space"]))
        if disk["type"] != "Local" or total == 0 or free > total:
            raise ValueError("invalid default local disk")
        ttl_days = {}
        for row in rows(
            "SELECT name, engine_full FROM system.tables "
            f"WHERE database = 'system' AND name IN ({names})"
        ):
            table = row["name"]
            if table not in CLICKHOUSE_LOG_TABLES or table in ttl_days:
                raise ValueError("unexpected system table")
            ttl_days[table] = _clickhouse_log_ttl_days(row["engine_full"])
        part_bytes = {}
        for row in rows(
            "SELECT table, sum(bytes_on_disk) AS active_bytes FROM system.parts "
            f"WHERE active AND database = 'system' AND match(table, {_sql_literal(family)}) GROUP BY table"
        ):
            table = row["table"]
            if not re.fullmatch(family, table) or table in part_bytes:
                raise ValueError("unexpected system parts table")
            part_bytes[table] = _clickhouse_byte_count(row["active_bytes"])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f"ClickHouse retention metadata unreadable on pod {pod}") from error

    after = _clickhouse_writer_snapshot(runner, namespace, pod)
    evidence = re.fullmatch(r"[0-9a-f]{64}\t(active|disabled|unverified)\t(active|disabled|unverified)", before)
    states = evidence.groups() if evidence and before == after else ("unverified", "unverified")
    writers = dict(zip(CLICKHOUSE_REMOVABLE_WRITERS, states, strict=True))

    failures = []
    for table in CLICKHOUSE_LOG_TABLES:
        if writers.get(table) == "disabled":
            continue
        days = ttl_days.get(table)
        if table not in ttl_days:
            failures.append(f"{table} is missing")
        elif days is None:
            failures.append(f"{table} has no supported unconditional deletion TTL")
        elif days != CLICKHOUSE_LOG_RETENTION_DAYS:
            failures.append(
                f"{table} has a {days}-day deletion TTL. Expected {CLICKHOUSE_LOG_RETENTION_DAYS} days."
            )
    used = total - free
    tracked_bytes = sum(part_bytes.values())
    if used * 100 > total * MAX_CLICKHOUSE_DISK_USED_PERCENT:
        failures.append(f"Disk use of {used}/{total} bytes exceeds {MAX_CLICKHOUSE_DISK_USED_PERCENT}%")
    if tracked_bytes > MAX_CLICKHOUSE_LOG_BYTES:
        failures.append(f"Active system log parts use {tracked_bytes} bytes. Limit is {MAX_CLICKHOUSE_LOG_BYTES}.")
    return {
        "diskTotalBytes": total,
        "diskFreeBytes": free,
        "diskUsedPercent": round(used * 100 / total, 2),
        "deletionTtlDays": ttl_days,
        "logWriters": writers,
        "retainedDisabledLogBytes": sum(
            size for table, size in part_bytes.items()
            if writers.get(re.sub(r"_[0-9]+$", "", table)) == "disabled"
        ),
        "activeLogBytesByTable": part_bytes,
        "trackedLogBytes": tracked_bytes,
        "rotatedLogBytes": sum(size for table, size in part_bytes.items() if table not in CLICKHOUSE_LOG_TABLES),
        "failures": failures,
    }


def _read_clickhouse_retention_pods(
    runner: Runner, namespace: str, pods: list[str],
) -> tuple[dict[str, dict[str, Any]], str]:
    snapshots = {pod: _read_clickhouse_retention(runner, namespace, pod) for pod in pods}
    failed = ", ".join(pod for pod, snapshot in snapshots.items() if snapshot["failures"])
    return snapshots, failed


def _wait_for_readback(
    runner: Runner,
    namespace: str,
    pod: str,
    environment: str,
    acceptance_id: str,
    attempts: int,
    interval_seconds: float,
) -> dict[str, int]:
    queries = build_readback_queries(environment, acceptance_id)
    counts: dict[str, int] = {}
    for attempt in range(1, attempts + 1):
        counts = {
            signal: _query_integer(runner, namespace, pod, query)
            for signal, query in queries.items()
        }
        missing = [signal for signal, count in counts.items() if count < 1]
        if not missing:
            return counts
        if attempt < attempts:
            time.sleep(interval_seconds)
    raise RuntimeError(
        "synthetic OTLP readback failed for: "
        + ", ".join(signal for signal, count in counts.items() if count < 1)
    )


def _wait_for_infra_readback(
    runner: Runner,
    namespace: str,
    pod: str,
    environment: str,
    cluster_name: str,
    acceptance_id: str,
    trace_id: str,
    forbidden_marker: str,
    attempts: int,
    interval_seconds: float,
) -> dict[str, int]:
    queries = build_infra_readback_queries(
        environment,
        cluster_name,
        acceptance_id,
        trace_id,
        forbidden_marker,
    )
    counts: dict[str, int] = {}
    for attempt in range(1, attempts + 1):
        counts = {
            signal: _query_integer(runner, namespace, pod, query)
            for signal, query in queries.items()
        }
        failures = infra_readback_failures(counts)
        if not failures:
            return counts
        if attempt < attempts:
            time.sleep(interval_seconds)
    raise RuntimeError("Kubernetes signal readback failed: " + ", ".join(failures))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--cluster-name")
    parser.add_argument("--ssh-host")
    parser.add_argument(
        "--argocd-application", metavar="NAMESPACE/NAME",
        help="Check the in-cluster Argo CD Application for a completed migration Sync hook",
    )
    parser.add_argument(
        "--argocd-migration-job",
        help=f"Rendered Argo CD migration Job name when it differs from {ARGOCD_MIGRATION_JOB}",
    )
    parser.add_argument("--signoz-url", help="Query the trusted HTTPS SigNoz API in Java signal mode")
    parser.add_argument("--since-unix-ms", type=int, help="Earliest admitted sample in Java signal mode")
    parser.add_argument("--timeout", default="10m")
    parser.add_argument("--readback-attempts", type=int, default=20)
    parser.add_argument("--readback-interval-seconds", type=float, default=3)
    parser.add_argument("--infra-readback-attempts", type=int, default=40)
    parser.add_argument(
        "--skip-k8s-infra",
        action="store_true",
        help="Check the backend before infrastructure and Java signal acceptance",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--skip-synthetic",
        action="store_true",
        help="Check readiness and pipeline wiring without proving persistence",
    )
    mode.add_argument(
        "--check-java-signals",
        action="store_true",
        help="Read stored Java signals and correlation without creating acceptance probes",
    )
    mode.add_argument(
        "--check-clickhouse-retention",
        action="store_true",
        help="Read ClickHouse deletion TTLs and disk and system log usage without creating acceptance probes",
    )
    args = parser.parse_args()
    if (args.signoz_url is not None or args.since_unix_ms is not None) and not args.check_java_signals:
        parser.error("--signoz-url and --since-unix-ms require --check-java-signals")
    if args.signoz_url is not None and args.ssh_host is not None:
        parser.error("--signoz-url and --ssh-host select different query transports")
    if args.argocd_application is not None:
        if not re.fullmatch(r"[a-z0-9-]+/[a-z0-9.-]+", args.argocd_application):
            parser.error("--argocd-application must be NAMESPACE/NAME")
        if args.check_java_signals or args.check_clickhouse_retention:
            parser.error("--argocd-application requires a readiness or full acceptance check")
    if args.argocd_migration_job is not None and args.argocd_application is None:
        parser.error("--argocd-migration-job requires --argocd-application")
    return args


def main() -> None:
    args = parse_args()
    if args.check_java_signals:
        if args.signoz_url is not None:
            signals = _read_java_signals_api(args.signoz_url, args.environment, args.since_unix_ms)
        else:
            runner = Runner(args.ssh_host)
            pod = _clickhouse_pods(runner, args.namespace, args.release)[0]
            signals = _read_java_signals(runner, args.namespace, pod, args.environment, args.since_unix_ms)
        print(json.dumps(signals, indent=2, sort_keys=True))
        _require_java_signals(signals)
        return

    runner = Runner(args.ssh_host)
    if args.check_clickhouse_retention:
        pods = _clickhouse_pods(runner, args.namespace, args.release)
        snapshots, failed = _read_clickhouse_retention_pods(runner, args.namespace, pods)
        print(json.dumps({"environment": args.environment, "pods": snapshots}, indent=2, sort_keys=True))
        if failed:
            raise RuntimeError(f"ClickHouse retention conformance failed on {failed}")
        return

    require_k8s_infra = not args.skip_k8s_infra
    if require_k8s_infra and not args.cluster_name:
        raise RuntimeError("--cluster-name is required unless --skip-k8s-infra is set")
    argocd_migration = _check_runtime_readiness(
        runner,
        args.namespace,
        args.release,
        args.timeout,
        require_k8s_infra=require_k8s_infra,
        argocd_application=args.argocd_application,
        argocd_migration_job=args.argocd_migration_job or ARGOCD_MIGRATION_JOB,
    )
    _check_collector_config(runner, args.namespace, args.release)

    report: dict[str, Any] = {
        "namespace": args.namespace,
        "release": args.release,
        "environment": args.environment,
        "readiness": "passed",
        "collectorPipeline": "passed",
        "k8sInfraReadiness": "passed" if require_k8s_infra else "skipped",
        "clickhouseRetention": "skipped",
    }
    if argocd_migration is not None and args.argocd_application is not None:
        report["argocdMigration"] = argocd_migration
    if args.skip_synthetic:
        report["clickhouseCapacity"] = "skipped"
        report["syntheticReadback"] = "skipped"
        report["k8sInfraReadback"] = "skipped"
    else:
        clickhouse_pods = _clickhouse_pods(runner, args.namespace, args.release)
        if require_k8s_infra:
            snapshots, failed = _read_clickhouse_retention_pods(runner, args.namespace, clickhouse_pods)
            report["clickhouseRetention"] = {
                "status": "failed" if failed else "passed",
                "pods": snapshots,
            }
            if failed:
                report["clickhouseCapacity"] = "not_reached"
                print(json.dumps(report, indent=2, sort_keys=True))
                raise RuntimeError(f"ClickHouse retention conformance failed on {failed}")
        for pod in clickhouse_pods:
            _check_clickhouse_capacity(runner, args.namespace, pod)
        readback_pod = clickhouse_pods[0]
        report["clickhouseCapacity"] = "passed"
        acceptance_id = f"acceptance-{uuid.uuid4().hex}"
        timestamp_ns = time.time_ns()
        trace_id = secrets.token_hex(16)
        span_id = secrets.token_hex(8)
        payloads = build_otlp_payloads(
            acceptance_id=acceptance_id,
            environment=args.environment,
            timestamp_ns=timestamp_ns,
            trace_id=trace_id,
            span_id=span_id,
        )
        for signal, payload in payloads.items():
            _emit_signal(
                runner,
                args.namespace,
                args.release,
                acceptance_id,
                signal,
                payload,
            )
        report["syntheticReadback"] = _wait_for_readback(
            runner,
            args.namespace,
            readback_pod,
            args.environment,
            acceptance_id,
            args.readback_attempts,
            args.readback_interval_seconds,
        )
        probe_pod: str | None = None
        if require_k8s_infra:
            forbidden_marker = f"oriso-private-probe-{secrets.token_hex(8)}"
            try:
                probe_pod = _emit_infra_probes(
                    runner,
                    args.namespace,
                    acceptance_id,
                    trace_id,
                    span_id,
                    forbidden_marker,
                )
                report["k8sInfraReadback"] = _wait_for_infra_readback(
                    runner,
                    args.namespace,
                    readback_pod,
                    args.environment,
                    args.cluster_name,
                    acceptance_id,
                    trace_id,
                    forbidden_marker,
                    args.infra_readback_attempts,
                    args.readback_interval_seconds,
                )
            finally:
                if probe_pod:
                    _delete_probe_pod(runner, args.namespace, probe_pod)
            signals = _read_java_signals(runner, args.namespace, readback_pod, args.environment)
            _require_java_signals(signals)
            report["javaSignals"] = signals
        else:
            report["k8sInfraReadback"] = "skipped"
        report["acceptanceId"] = acceptance_id

    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, ValueError) as error:
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr.strip(), file=sys.stderr)
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
