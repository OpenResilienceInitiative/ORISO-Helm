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
import secrets
import shlex
import subprocess
import sys
import time
import uuid
from typing import Any

import yaml

SERVICE_NAME = "oriso-signoz-acceptance"
METRIC_NAME = "oriso.signoz.acceptance"
SUPPRESSED_LOG_BODY = "[ORISO log body suppressed by privacy policy]"
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
) -> None:
    chi_name = f"{release}-clickhouse"
    chi = json.loads(_kubectl(runner, namespace, "get", "chi", chi_name, "-o", "json"))
    if chi.get("metadata", {}).get("deletionTimestamp"):
        raise RuntimeError(f"ClickHouseInstallation {chi_name} is deleting")

    rollout_targets = (
        f"deployment/{release}-clickhouse-operator",
        f"statefulset/{release}-clickhouse-cluster-0-0",
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

    _kubectl(
        runner,
        namespace,
        "wait",
        "--for=condition=complete",
        "job/signoz-telemetrystore-migrator",
        f"--timeout={timeout}",
    )


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
    event_name = f"signoz-acceptance-{suffix}"
    _kubectl(
        runner,
        namespace,
        "create",
        "event",
        event_name,
        "--reason=SigNozAcceptance",
        "--type=Normal",
        f"--message={acceptance_id}",
        f"--for=namespace/{namespace}",
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


def _clickhouse_pod(runner: Runner, namespace: str, release: str) -> str:
    pod = _kubectl(
        runner,
        namespace,
        "get",
        "pod",
        "-l",
        f"app.kubernetes.io/instance={release},app.kubernetes.io/component=clickhouse",
        "-o",
        "jsonpath={.items[0].metadata.name}",
    )
    if not pod:
        raise RuntimeError("ClickHouse pod was not found")
    return pod


def _query_count(runner: Runner, namespace: str, pod: str, query: str) -> int:
    output = _kubectl(
        runner,
        namespace,
        "exec",
        pod,
        "--",
        "clickhouse-client",
        "--format",
        "TabSeparatedRaw",
        "--query",
        query,
    )
    return int(output.splitlines()[-1])


def _wait_for_readback(
    runner: Runner,
    namespace: str,
    release: str,
    environment: str,
    acceptance_id: str,
    attempts: int,
    interval_seconds: float,
) -> dict[str, int]:
    pod = _clickhouse_pod(runner, namespace, release)
    queries = build_readback_queries(environment, acceptance_id)
    counts: dict[str, int] = {}
    for attempt in range(1, attempts + 1):
        counts = {
            signal: _query_count(runner, namespace, pod, query)
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
    release: str,
    environment: str,
    cluster_name: str,
    acceptance_id: str,
    trace_id: str,
    forbidden_marker: str,
    attempts: int,
    interval_seconds: float,
) -> dict[str, int]:
    pod = _clickhouse_pod(runner, namespace, release)
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
            signal: _query_count(runner, namespace, pod, query)
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
    parser.add_argument("--timeout", default="10m")
    parser.add_argument("--readback-attempts", type=int, default=20)
    parser.add_argument("--readback-interval-seconds", type=float, default=3)
    parser.add_argument("--infra-readback-attempts", type=int, default=40)
    parser.add_argument(
        "--skip-k8s-infra",
        action="store_true",
        help="Skip k8s-infra readiness and signal-quality acceptance",
    )
    parser.add_argument(
        "--skip-synthetic",
        action="store_true",
        help="Check readiness and pipeline wiring without proving persistence",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    require_k8s_infra = not args.skip_k8s_infra
    if require_k8s_infra and not args.cluster_name:
        raise RuntimeError("--cluster-name is required unless --skip-k8s-infra is set")
    runner = Runner(args.ssh_host)
    _check_runtime_readiness(
        runner,
        args.namespace,
        args.release,
        args.timeout,
        require_k8s_infra=require_k8s_infra,
    )
    _check_collector_config(runner, args.namespace, args.release)

    report: dict[str, Any] = {
        "namespace": args.namespace,
        "release": args.release,
        "environment": args.environment,
        "readiness": "passed",
        "collectorPipeline": "passed",
        "k8sInfraReadiness": "passed" if require_k8s_infra else "skipped",
    }
    if args.skip_synthetic:
        report["syntheticReadback"] = "skipped"
        report["k8sInfraReadback"] = "skipped"
    else:
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
            args.release,
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
                    args.release,
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
