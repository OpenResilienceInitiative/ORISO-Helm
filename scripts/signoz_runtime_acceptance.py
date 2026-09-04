#!/usr/bin/env python3
"""Prove that ORISO SigNoz is ready and persists all three OTLP signals.

The default mode is an active, privacy-safe acceptance check: it emits one
synthetic trace, metric and correlated log through the in-cluster OTLP/HTTP
receiver, then reads all three signals back from ClickHouse. No credentials or
application payloads are printed or stored.
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
                                        _attribute(
                                            "oriso.acceptance.id", acceptance_id
                                        )
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
                                        _attribute(
                                            "oriso.acceptance.id", acceptance_id
                                        )
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


def _sql_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


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
        "metrics": f"""
            SELECT count() FROM signoz_metrics.time_series_v4
            WHERE metric_name = {metric}
              AND resource_attrs['service.name'] = {service}
              AND resource_attrs['deployment.environment'] = {env}
              AND resource_attrs['oriso.acceptance.id'] = {acceptance}
        """.strip(),
        "logs": f"""
            SELECT count() FROM signoz_logs.logs_v2
            WHERE timestamp > toUnixTimestamp(now() - INTERVAL 15 MINUTE) * 1000000000
              AND resources_string['service.name'] = {service}
              AND resources_string['deployment.environment'] = {env}
              AND resources_string['oriso.acceptance.id'] = {acceptance}
        """.strip(),
    }


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


def _clickhouse_statefulset(runner: Runner, namespace: str, release: str) -> str:
    payload = json.loads(
        _kubectl(
            runner,
            namespace,
            "get",
            "statefulset",
            "-l",
            f"clickhouse.altinity.com/chi={release}-clickhouse",
            "-o",
            "json",
        )
    )
    names = [item.get("metadata", {}).get("name") for item in payload.get("items", [])]
    names = [name for name in names if name]
    if len(names) != 1:
        raise RuntimeError(
            "expected exactly one ClickHouse StatefulSet, found " + str(len(names))
        )
    return names[0]


def _check_runtime_readiness(
    runner: Runner, namespace: str, release: str, timeout: str
) -> None:
    chi_name = f"{release}-clickhouse"
    chi = json.loads(_kubectl(runner, namespace, "get", "chi", chi_name, "-o", "json"))
    if chi.get("metadata", {}).get("deletionTimestamp"):
        raise RuntimeError(f"ClickHouseInstallation {chi_name} is deleting")

    clickhouse_statefulset = _clickhouse_statefulset(runner, namespace, release)
    rollout_targets = (
        f"deployment/{release}-clickhouse-operator",
        f"statefulset/{clickhouse_statefulset}",
        f"deployment/{release}-signoz-otel-collector",
        f"statefulset/{release}-signoz",
    )
    for target in rollout_targets:
        _kubectl(runner, namespace, "rollout", "status", target, f"--timeout={timeout}")

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


def _query_count(
    runner: Runner, namespace: str, pod: str, query: str
) -> int:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--ssh-host")
    parser.add_argument("--timeout", default="10m")
    parser.add_argument("--readback-attempts", type=int, default=20)
    parser.add_argument("--readback-interval-seconds", type=float, default=3)
    parser.add_argument(
        "--skip-synthetic",
        action="store_true",
        help="Check readiness and pipeline wiring without proving persistence",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    runner = Runner(args.ssh_host)
    _check_runtime_readiness(runner, args.namespace, args.release, args.timeout)
    _check_collector_config(runner, args.namespace, args.release)

    report: dict[str, Any] = {
        "namespace": args.namespace,
        "release": args.release,
        "environment": args.environment,
        "readiness": "passed",
        "collectorPipeline": "passed",
    }
    if args.skip_synthetic:
        report["syntheticReadback"] = "skipped"
    else:
        acceptance_id = f"acceptance-{uuid.uuid4().hex}"
        timestamp_ns = time.time_ns()
        payloads = build_otlp_payloads(
            acceptance_id=acceptance_id,
            environment=args.environment,
            timestamp_ns=timestamp_ns,
            trace_id=secrets.token_hex(16),
            span_id=secrets.token_hex(8),
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
