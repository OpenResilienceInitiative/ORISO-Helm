#!/usr/bin/env python3
"""Rehearse the ordinary OTLP boundary with a pinned SigNoz release archive.

Supply the synthetic Dev Helm render and the v0.144.6 release archive for
Darwin ARM64 or Linux AMD64. All listeners, captures and payloads are local.
Use --require-safe-errors to fail when a response echoes a canary.
"""

import argparse
import copy
import hashlib
import http.client
import json
import platform
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import yaml


SIGNALS = ("traces", "metrics", "logs")
IMAGE = "docker.io/signoz/signoz-otel-collector:v0.144.6"
ARCHIVES = {
    ("Darwin", "arm64"): (
        "signoz-otel-collector_darwin_arm64",
        "493ad6b899e2aa28304b9fac44c80104783adfaf90f051c0d2b32fbed82ffc42",
    ),
    ("Linux", "x86_64"): (
        "signoz-otel-collector_linux_amd64",
        "a02e2e3ad4a2a62db7ca50c145d1cfcd4e9decaefb34b5565366ab81092fe374",
    ),
}
POLICY = Path(__file__).parent / "fixtures" / "signoz-collector-privacy-rehearsal.yaml"
SERVICE = "oriso-privacy-fixture"
METRIC = "oriso.privacy.rehearsal.ms"
PREFIX = "privacy-canary-"
NOW = str(time.time_ns())


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def release_collector(archive, directory):
    runtime = (platform.system(), platform.machine())
    if runtime not in ARCHIVES:
        raise ValueError(f"Unsupported rehearsal platform {runtime}")
    name, digest = ARCHIVES[runtime]
    collector = directory / "signoz-otel-collector"
    with archive.open("rb") as asset:
        check(hashlib.file_digest(asset, "sha256").hexdigest() == digest, f"Wrong digest for {name}.tar.gz")
        asset.seek(0)
        with tarfile.open(fileobj=asset, mode="r:gz") as release:
            member = release.getmember(f"{name}/bin/signoz-otel-collector")
            check(member.isfile(), "Collector archive member must be a regular file")
            with release.extractfile(member) as binary, collector.open("xb") as output:
                shutil.copyfileobj(binary, output)
    collector.chmod(0o700)
    return collector


def rendered_collector(manifest):
    name = "caritas-signoz-otel-collector"
    with manifest.open(encoding="utf-8") as source:
        resources = {
            doc["kind"]: doc
            for doc in yaml.safe_load_all(source)
            if isinstance(doc, dict) and doc.get("metadata", {}).get("name") == name
        }
    pod = resources["Deployment"]["spec"]["template"]["spec"]
    image = next(container["image"] for container in pod["containers"] if container["name"] == "collector")
    check(image == IMAGE, f"Expected the rendered Collector image {IMAGE}")
    return yaml.safe_load(resources["ConfigMap"]["data"]["otel-collector-config.yaml"])


def attribute(key, value):
    if isinstance(value, str):
        value = {"stringValue": value}
    elif isinstance(value, int):
        value = {"intValue": str(value)}
    return {"key": key, "value": value}


def envelope(signal, records):
    collection = "Spans" if signal == "traces" else signal.title()
    return {
        "resource" + collection: [{
            "schemaUrl": "https://example.invalid/" + PREFIX + "resource-schema",
            "resource": {"attributes": [
                attribute("service.name", SERVICE),
                attribute("service.namespace", PREFIX + "namespace"),
                attribute("deployment.environment", PREFIX + "environment"),
                attribute("user.id", PREFIX + "resource-attribute"),
            ]},
            "scope" + collection: [{
                "schemaUrl": "https://example.invalid/" + PREFIX + "scope-schema",
                "scope": {
                    "name": PREFIX + "scope-name", "version": PREFIX + "scope-version",
                    "attributes": [attribute("scope.extra", PREFIX + "scope-attribute")],
                },
                {"traces": "spans", "metrics": "metrics", "logs": "logRecords"}[signal]: records,
            }],
        }]
    }


def span(number, route="/canary/health", status=200):
    return {
        "traceId": f"{number:032x}", "spanId": f"{number:016x}", "kind": 2,
        "name": PREFIX + "span-name",
        "traceState": "canary=" + PREFIX + "trace-state",
        "startTimeUnixNano": NOW, "endTimeUnixNano": str(int(NOW) + 1_000_000),
        "attributes": [
            attribute("http.request.method", "GET"),
            attribute("http.route", route),
            attribute("http.response.status_code", status),
            attribute("url.full", "https://example.invalid/?user=" + PREFIX + "url-query"),
        ],
        "status": {"code": 2, "message": PREFIX + "span-status"},
        "events": [{
            "timeUnixNano": NOW, "name": PREFIX + "span-event",
            "attributes": [attribute("exception.message", PREFIX + "event-attribute")],
        }],
    }


def payloads():
    linked = span(2)
    linked["links"] = [{
        "traceId": "f" * 32, "spanId": "f" * 16,
        "traceState": "canary=" + PREFIX + "link-state",
        "attributes": [attribute("peer.id", PREFIX + "link-attribute")],
    }]
    invalid_type = {"kvlistValue": {"values": [attribute("nested", PREFIX + "bad-status")]}}
    traces = envelope("traces", [span(1), linked, span(3, status=invalid_type), span(4, route="/" + PREFIX + "raw-route")])

    point = {
        "timeUnixNano": NOW, "asDouble": 2.5,
        "attributes": [attribute("client.address", PREFIX + "datapoint-attribute")],
    }
    exemplar = {
        **point, "asDouble": 8.5,
        "exemplars": [{
            "timeUnixNano": NOW, "asDouble": 8.5, "traceId": "f" * 32, "spanId": "f" * 16,
            "filteredAttributes": [attribute("peer.id", PREFIX + "exemplar-attribute")],
        }],
    }
    metric = {
        "name": METRIC, "description": PREFIX + "metric-description",
        "unit": PREFIX + "metric-unit", "gauge": {"dataPoints": [point, exemplar]},
    }
    metrics = envelope("metrics", [metric, {**metric, "name": PREFIX + "metric-name"}])
    logs = envelope("logs", [{
        "timeUnixNano": NOW, "severityNumber": 17, "severityText": PREFIX + "severity-text",
        "eventName": PREFIX + "log-event",
        "body": {"kvlistValue": {"values": [attribute("content", PREFIX + "log-body")]}},
        "attributes": [attribute("session.id", PREFIX + "log-attribute")],
    }])
    return {"traces": traces, "metrics": metrics, "logs": logs}


def capture_config(base, directory, port, policy):
    source = base["service"]["pipelines"]
    active = ("traces", "metrics") if policy else SIGNALS
    allowed = {"memory_limiter", "batch", "signozspanmetrics/delta"}
    processors = {}
    pipelines = {}
    for signal in active:
        chain = source[signal]["processors"]
        if set(chain) - allowed or source[signal]["receivers"] != ["otlp"]:
            raise ValueError(f"{signal} no longer uses the supported ordinary OTLP baseline")
        processors.update({name: copy.deepcopy(base["processors"][name]) for name in chain})
        if policy:
            boundary = int(chain[0] == "memory_limiter")
            chain = [*chain[:boundary], "filter/privacy", "transform/privacy", *chain[boundary:]]
        pipelines[signal] = {"receivers": ["otlp"], "processors": chain, "exporters": [f"file/{signal}"]}
    delta = processors["signozspanmetrics/delta"]
    delta.update(metrics_exporter="file/derived", metrics_flush_interval="500ms")
    pipelines["metrics/derived"] = {"receivers": ["otlp/derived"], "exporters": ["file/derived"]}
    processors.update(policy)
    return {
        "receivers": {
            "otlp": {"protocols": {"http": {"endpoint": f"127.0.0.1:{port}"}}},
            "otlp/derived": {"protocols": {"http": {"endpoint": "127.0.0.1:0"}}},
        },
        "processors": processors,
        "exporters": {f"file/{name}": {"path": str(directory / f"{name}.json")} for name in (*active, "derived")},
        "service": {"telemetry": {"metrics": {"level": "none"}}, "pipelines": pipelines},
    }


def post(port, signal, body, content_type="application/json"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("POST", f"/v1/{signal}", body, {"Content-Type": content_type})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def run(collector, base, inputs, policy, *, inject_processor_error=False):
    if inject_processor_error:
        policy = copy.deepcopy(policy)
        # A valid OTLP string contains invalid JSON so OTTL propagate is exercised.
        policy["filter/privacy"]["traces"]["span"].insert(0, (
            'span.attributes["probe.error"] != nil and ParseJSON(span.attributes["probe.error"]) != nil'
        ))
    with tempfile.TemporaryDirectory(prefix="signoz-privacy-capture-") as scratch:
        directory = Path(scratch)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        config = directory / "collector.yaml"
        config.write_text(yaml.safe_dump(capture_config(base, directory, port, policy)), encoding="utf-8")
        diagnostics = directory / "diagnostics.txt"
        with diagnostics.open("wb") as log:
            process = subprocess.Popen(
                [str(collector), "--config", str(config), "--copy-path", str(directory / "copy.yaml")],
                env={}, stdout=log, stderr=subprocess.STDOUT,
            )
            responses = {}
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and process.poll() is None:
                    if "Everything is ready. Begin running and processing data." in diagnostics.read_text(encoding="utf-8"):
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("collector did not start")
                for signal, body in inputs.items():
                    responses[signal] = post(port, signal, json.dumps(body))
                responses["malformed_json"] = post(port, "traces", '{"broken":' + PREFIX + 'broken')
                responses["malformed_protobuf"] = post(
                    port, "traces", b"\x0a\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff" + PREFIX.encode(),
                    "application/x-protobuf",
                )
                if inject_processor_error:
                    failing = span(5)
                    failing["attributes"].append(attribute("probe.error", "!" + PREFIX + "processor-error"))
                    responses["processor_error"] = post(port, "traces", json.dumps(envelope("traces", [failing])))
                required = ("traces", "metrics", "derived") if policy else (*SIGNALS, "derived")
                capture_paths = [directory / f"{name}.json" for name in required]
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and process.poll() is None:
                    if all(file.is_file() and file.stat().st_size > 0 for file in capture_paths):
                        break
                    time.sleep(0.1)
                else:
                    statuses = {signal: status for signal, (status, _) in responses.items()}
                    sizes = {file.stem: file.stat().st_size for file in directory.glob("*.json")}
                    raise RuntimeError(f"incomplete capture, responses {statuses}, file sizes {sizes}")
            except (RuntimeError, OSError) as error:
                raise RuntimeError(f"{error}\n{diagnostics.read_text(encoding='utf-8')[-3500:]}") from error
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise RuntimeError("collector did not shut down after capture")
        captures = {file.stem: file.read_text(encoding="utf-8") for file in directory.glob("*.json")}
        return responses, captures, diagnostics.read_text(encoding="utf-8")


def records(capture, collection, field):
    return [
        record
        for line in capture.splitlines()
        for resource in json.loads(line)["resource" + collection]
        for scope in resource["scope" + collection]
        for record in scope[field]
    ]


def verify_baseline(responses, captures):
    expected = {
        "traces": ("span-name", "resource-schema", "scope-schema", "scope-attribute",
                   "trace-state", "span-status", "event-attribute", "link-state", "link-attribute",
                   "bad-status", "raw-route", "url-query"),
        "metrics": ("metric-name", "metric-description", "metric-unit", "datapoint-attribute", "exemplar-attribute"),
        "logs": ("log-body", "log-attribute", "log-event", "severity-text"),
        "derived": ("span-name", "namespace", "environment"),
    }
    for signal in SIGNALS:
        check(responses[signal][0] == 200, f"Baseline rejected the {signal} canary")
    for stream, markers in expected.items():
        missing = [name for name in markers if PREFIX + name not in captures[stream]]
        check(not missing, f"Baseline {stream} capture did not observe markers {missing}")


def verify_fixture(responses, captures, diagnostics):
    expected = {"traces": 200, "metrics": 200, "logs": 404, "malformed_json": 400, "malformed_protobuf": 400}
    for request, status in expected.items():
        check(responses[request][0] == status, f"Unexpected {request} response {responses[request][0]}")
    check(set(captures) == {"traces", "metrics", "derived"}, "Unexpected fixture capture streams")
    for stream, contents in {**captures, "collector diagnostics": diagnostics}.items():
        check(PREFIX not in contents, f"Synthetic marker reached {stream}")

    spans = records(captures["traces"], "Spans", "spans")
    check([span["spanId"] for span in spans] == ["0000000000000001"], "Unexpected retained spans")
    kept = spans[0]
    check(kept["name"] == "GET /canary/health" and not kept.get("events") and not kept.get("links"),
          "Retained span was not normalized")
    check(kept["attributes"] == [attribute("http.response.status_code", 200)], "Unexpected retained attributes")

    metrics = records(captures["metrics"], "Metrics", "metrics")
    check([metric["name"] for metric in metrics] == [METRIC], "Unexpected retained metrics")
    points = metrics[0]["gauge"]["dataPoints"]
    check(len(points) == 1 and points[0]["asDouble"] == 2.5 and not points[0].get("exemplars"),
          "Unexpected retained metric points")

    calls = [
        item for metric in records(captures["derived"], "Metrics", "metrics") if metric["name"] == "signoz_calls_total"
        for point in metric["sum"].get("dataPoints", [])
        for item in point["attributes"] if item["key"] == "operation"
    ]
    check(calls and all(item == attribute("operation", "GET /canary/health") for item in calls),
          "Span-derived metrics missed the normalized operation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="Pinned SigNoz Collector release archive")
    parser.add_argument("manifest", type=Path, help="Synthetic Helm render of the Dev collector")
    parser.add_argument("--require-safe-errors", action="store_true")
    args = parser.parse_args()
    base = rendered_collector(args.manifest)
    policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="signoz-privacy-runtime-") as directory:
        collector = release_collector(args.archive, Path(directory))
        baseline_responses, baseline_captures, _ = run(collector, base, payloads(), {})
        verify_baseline(baseline_responses, baseline_captures)
        print("Observed synthetic markers in ordinary trace, metric, log and derived-metric exports")

        responses, captures, diagnostics = run(collector, base, payloads(), policy)
        verify_fixture(responses, captures, diagnostics)
        print("Fixture retained one normalized span and gauge, excluded the denied records and disabled log intake")

        fault_responses, fault_captures, fault_diagnostics = run(
            collector, base, payloads(), policy, inject_processor_error=True,
        )
        verify_fixture(fault_responses, fault_captures, fault_diagnostics)
        status = fault_responses["processor_error"][0]
        check(400 <= status < 600, f"OTTL processing fault was accepted with HTTP {status}")
        print(f"Injected OTTL fault returned HTTP {status} and did not reach the exports")

    errors = {**responses, "processor_error": fault_responses["processor_error"]}
    reflected = [name for name, (status, body) in errors.items() if status >= 400 and PREFIX.encode() in body]
    if reflected:
        print(f"OPEN receiver boundary. Request markers reflected in {', '.join(reflected)}")
        if args.require_safe_errors:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
