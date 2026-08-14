#!/usr/bin/env python3
"""Send and read back one uniquely marked OTLP trace, metric, and log."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:-]+$")
METRIC_NAME = "oriso.cutover.canary"


def string_attribute(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def build_otlp_payloads(
    run_id: str,
    environment: str,
    service_name: str,
    timestamp_ns: int,
    trace_id: str,
    span_id: str,
) -> dict[str, dict[str, Any]]:
    resource = {
        "attributes": [
            string_attribute("service.name", service_name),
            string_attribute("deployment.environment", environment),
            string_attribute("oriso.cutover.run_id", run_id),
        ]
    }
    marker = string_attribute("oriso.cutover.run_id", run_id)
    return {
        "traces": {
            "resourceSpans": [
                {
                    "resource": resource,
                    "scopeSpans": [
                        {
                            "scope": {"name": "oriso-cutover-verifier"},
                            "spans": [
                                {
                                    "traceId": trace_id,
                                    "spanId": span_id,
                                    "name": "matrixrtc-cutover-verification",
                                    "kind": 2,
                                    "startTimeUnixNano": str(timestamp_ns),
                                    "endTimeUnixNano": str(timestamp_ns + 1_000_000),
                                    "attributes": [marker],
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
                            "scope": {"name": "oriso-cutover-verifier"},
                            "metrics": [
                                {
                                    "name": METRIC_NAME,
                                    "description": "ORISO cutover ingestion proof",
                                    "unit": "1",
                                    "gauge": {
                                        "dataPoints": [
                                            {
                                                "timeUnixNano": str(timestamp_ns),
                                                "asDouble": 1.0,
                                                "attributes": [marker],
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
                            "scope": {"name": "oriso-cutover-verifier"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(timestamp_ns),
                                    "observedTimeUnixNano": str(timestamp_ns),
                                    "severityNumber": 9,
                                    "severityText": "INFO",
                                    "body": {
                                        "stringValue": f"ORISO cutover proof {run_id}"
                                    },
                                    "traceId": trace_id,
                                    "spanId": span_id,
                                    "attributes": [marker],
                                }
                            ],
                        }
                    ],
                }
            ]
        },
    }


def require_safe(value: str, label: str) -> str:
    if not SAFE_VALUE.fullmatch(value):
        raise ValueError(f"{label} contains unsafe query characters")
    return value


def build_query(
    signal: str,
    run_id: str,
    environment: str,
    service_name: str,
    start_ms: int,
    end_ms: int,
) -> dict[str, Any]:
    if signal not in {"traces", "metrics", "logs"}:
        raise ValueError(f"unsupported signal {signal!r}")
    run_id = require_safe(run_id, "run ID")
    environment = require_safe(environment, "environment")
    service_name = require_safe(service_name, "service name")
    expression = (
        f"service.name = '{service_name}' AND "
        f"deployment.environment = '{environment}' AND "
        f"oriso.cutover.run_id = '{run_id}'"
    )
    spec: dict[str, Any] = {
        "name": "A",
        "signal": signal,
        "filter": {"expression": expression},
        "disabled": False,
    }
    request_type = "raw"
    if signal == "metrics":
        request_type = "time_series"
        spec.update(
            {
                "stepInterval": 10,
                "aggregations": [
                    {
                        "metricName": METRIC_NAME,
                        "temporality": "Unspecified",
                        "timeAggregation": "avg",
                        "spaceAggregation": "max",
                    }
                ],
                "groupBy": [
                    {"name": "oriso.cutover.run_id"},
                    {"name": "deployment.environment"},
                    {"name": "service.name"},
                ],
            }
        )
    else:
        spec.update(
            {
                "selectFields": [
                    {"name": "oriso.cutover.run_id", "fieldContext": "resource"},
                    {"name": "deployment.environment", "fieldContext": "resource"},
                    {"name": "service.name", "fieldContext": "resource"},
                ],
                "limit": 10,
                "offset": 0,
            }
        )
    return {
        "start": start_ms,
        "end": end_ms,
        "requestType": request_type,
        "variables": {},
        "compositeQuery": {"queries": [{"type": "builder_query", "spec": spec}]},
    }


def contains_marker(value: Any, marker: str) -> bool:
    if isinstance(value, str):
        return marker in value
    if isinstance(value, dict):
        return any(
            contains_marker(key, marker) or contains_marker(item, marker)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(contains_marker(item, marker) for item in value)
    return False


def successful_readback_contains_marker(response: dict[str, Any], marker: str) -> bool:
    return response.get("status") == "success" and contains_marker(
        response.get("data"), marker
    )


def post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            body = response.read()
    except HTTPError as error:
        raise ValueError(f"HTTP {error.code} from {url}") from error
    except URLError as error:
        raise ValueError(f"cannot reach {url}: {error.reason}") from error
    if not body:
        return {}
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError as error:
        raise ValueError(f"non-JSON response from {url}") from error
    if not isinstance(decoded, dict):
        raise ValueError(f"unexpected response shape from {url}")
    return decoded


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_for_port(port: int, process: subprocess.Popen, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read().strip() if process.stderr else ""
            raise ValueError(f"kubectl port-forward stopped early: {stderr}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise ValueError(f"kubectl port-forward did not open local port {port}")


@contextlib.contextmanager
def port_forward(namespace: str, service: str, remote_port: int):
    local_port = free_local_port()
    process = subprocess.Popen(
        [
            "kubectl",
            "--namespace",
            namespace,
            "port-forward",
            f"service/{service}",
            f"{local_port}:{remote_port}",
            "--address",
            "127.0.0.1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for_port(local_port, process)
        yield f"http://127.0.0.1:{local_port}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def verify_ingestion(
    collector_url: str,
    signoz_url: str,
    api_key: str,
    run_id: str,
    environment: str,
    service_name: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    require_safe(run_id, "run ID")
    require_safe(environment, "environment")
    require_safe(service_name, "service name")
    if not api_key:
        raise ValueError("SIGNOZ_API_KEY is required for readback")

    started_ms = int(time.time() * 1000)
    timestamp_ns = started_ms * 1_000_000
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    payloads = build_otlp_payloads(
        run_id,
        environment,
        service_name,
        timestamp_ns,
        trace_id,
        span_id,
    )
    for signal, payload in payloads.items():
        post_json(f"{collector_url.rstrip('/')}/v1/{signal}", payload)

    query_url = f"{signoz_url.rstrip('/')}/api/v5/query_range"
    headers = {"SIGNOZ-API-KEY": api_key}
    pending = {"traces", "metrics", "logs"}
    deadline = time.monotonic() + timeout_seconds
    while pending and time.monotonic() < deadline:
        end_ms = int(time.time() * 1000) + 30_000
        for signal in tuple(pending):
            response = post_json(
                query_url,
                build_query(
                    signal,
                    run_id,
                    environment,
                    service_name,
                    started_ms - 60_000,
                    end_ms,
                ),
                headers,
            )
            if successful_readback_contains_marker(response, run_id):
                pending.remove(signal)
        if pending:
            time.sleep(min(5, max(0, deadline - time.monotonic())))
    if pending:
        raise ValueError(
            "SigNoz readback did not return the run marker for "
            + ", ".join(sorted(pending))
        )
    return {
        "runId": run_id,
        "environment": environment,
        "serviceName": service_name,
        "traceId": trace_id,
        "spanId": span_id,
        "ingestedSignals": ["traces", "metrics", "logs"],
        "readback": {"traces": True, "metrics": True, "logs": True},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", default="caritas")
    parser.add_argument("--namespace", default="caritas")
    parser.add_argument("--environment", default="predev")
    parser.add_argument("--service-name", default="oriso-cutover-canary")
    parser.add_argument("--run-id")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_id = args.run_id or time.strftime("cutover-%Y%m%d-%H%M%S", time.gmtime())
    api_key = os.environ.get("SIGNOZ_API_KEY", "")
    collector = f"{args.release}-signoz-otel-collector"
    signoz = f"{args.release}-signoz"
    try:
        with port_forward(args.namespace, collector, 4318) as collector_url:
            with port_forward(args.namespace, signoz, 8080) as signoz_url:
                evidence = verify_ingestion(
                    collector_url,
                    signoz_url,
                    api_key,
                    run_id,
                    args.environment,
                    args.service_name,
                    args.timeout_seconds,
                )
        rendered = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0
    except (OSError, TypeError, ValueError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
