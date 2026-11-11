"""Exercise the rendered private OTLP transport using the pinned Linux images."""

import base64
from collections import deque
from contextlib import contextmanager
import gzip
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from threading import Thread
import time
import uuid

from google.protobuf.json_format import ParseDict
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
import yaml

from render_signoz_private_otlp_test import BODY_LIMIT, NAME, PROXY_PORT, render_private

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from signoz_runtime_acceptance import build_otlp_payloads


MARKER = "private-otlp-synthetic-input"
PROTOBUF = "application/x-protobuf"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def docker(*args, check=True):
    result = subprocess.run(["docker", *args], text=True, capture_output=True, timeout=160)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    if args[0] == "logs":
        return f"{result.stdout}\n{result.stderr}".strip()
    return result.stdout.strip()


def request(host, port, path, data=None, headers=None, method=None):
    connection = http.client.HTTPConnection(host, port, timeout=4)
    try:
        connection.request(method or ("POST" if data is not None else "GET"), path, data, headers or {})
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


def ready(host, port, path, status):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        try:
            if request(host, port, path)[0] == status:
                return
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Runtime readiness probe did not reach {path}")


@contextmanager
def container(name, *arguments):
    docker("run", "--detach", "--name", name, *arguments)
    try:
        yield name
    except BaseException:
        print(docker("logs", name, check=False)[-3500:], file=sys.stderr)
        raise
    finally:
        docker("rm", "--force", name, check=False)


def proxy_arguments(proxy, config, network):
    return [
        "--network", network, "--user", "101:101", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:mode=1777,size=32m",
        "--volume", f"{config}:/conf:ro", "--entrypoint", proxy["command"][0],
        proxy["image"], *proxy["args"],
    ]


def isolated_config(manifest, config, sink):
    config.mkdir(mode=0o755)
    sink.mkdir()
    sink.chmod(0o777)
    data = manifest["data"]
    rendered = yaml.safe_load(data["otel-collector-config.yaml"])
    # Preserve the receiver and policy processors. Redirect terminal exporters
    # to scratch captures so the runtime test needs no ClickHouse credentials.
    rendered["exporters"] = {f"file/{signal}": {"path": f"/sink/{signal}.json"}
                             for signal in ("traces", "metrics", "meter")}
    pipelines = rendered["service"]["pipelines"]
    for signal in ("traces", "metrics"):
        pipelines[signal]["exporters"] = [f"file/{signal}", "signozmeter"]
    pipelines["metrics/meter"]["exporters"] = ["file/meter"]
    rendered["processors"]["signozspanmetrics/delta"].update(
        metrics_exporter="file/metrics", metrics_flush_interval="500ms",
    )
    (config / "otel-collector-config.yaml").write_text(yaml.safe_dump(rendered), encoding="utf-8")
    (config / "private-otlp.conf").write_text(data["private-otlp.conf"], encoding="utf-8")
    error = base64.b64decode(manifest["binaryData"]["private-otlp-error.pb"], validate=True)
    (config / "private-otlp-error.pb").write_bytes(error)
    return error


def payloads():
    fixtures = build_otlp_payloads("private-otlp", "rehearsal", time.time_ns(), uuid.uuid4().hex, uuid.uuid4().hex[:16])
    span = fixtures["traces"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    for field in ("traceId", "spanId"):
        span[field] = base64.b64encode(bytes.fromhex(span[field])).decode("ascii")
    span["name"] = MARKER
    good_trace = ParseDict(fixtures["traces"], ExportTraceServiceRequest()).SerializeToString()
    service = fixtures["traces"]["resourceSpans"][0]["resource"]["attributes"][0]["value"]
    original_service = service["stringValue"]
    service["stringValue"] = MARKER + "-other"
    denied_trace = ParseDict(fixtures["traces"], ExportTraceServiceRequest()).SerializeToString()
    service["stringValue"] = original_service
    span["attributes"].append({"key": "probe.error", "value": {"stringValue": "!" + MARKER}})
    processor_error = ParseDict(fixtures["traces"], ExportTraceServiceRequest()).SerializeToString()
    span["attributes"] = [{"key": "probe.large", "value": {"stringValue": "x" * (BODY_LIMIT * 2)}}]
    oversized = ParseDict(fixtures["traces"], ExportTraceServiceRequest()).SerializeToString()
    metrics = ParseDict(fixtures["metrics"], ExportMetricsServiceRequest()).SerializeToString()
    return good_trace, denied_trace, processor_error, oversized, metrics


def expect_error(result, expected, error):
    status, body, headers = result
    require(status == expected and body == error and headers.get("Content-Type") == PROTOBUF,
            f"Expected a fixed Protobuf error with status {expected}, received {status} {body[:160]!r}")


def verify_captures(sink):
    traces_file, metrics_file = sink / "traces.json", sink / "metrics.json"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        traces = traces_file.read_text(encoding="utf-8") if traces_file.exists() else ""
        metrics = metrics_file.read_text(encoding="utf-8") if metrics_file.exists() else ""
        if "synthetic private OTLP" in traces and "oriso.signoz.acceptance" in metrics and "signoz_calls_total" in metrics:
            spans = [
                span
                for line in traces.splitlines()
                for resource in json.loads(line)["resourceSpans"]
                for scope in resource["scopeSpans"]
                for span in scope["spans"]
            ]
            require(len(spans) == 1, "A denied or errored trace reached the capture")
            require(MARKER not in traces + metrics, "Raw synthetic data reached a terminal export")
            return
        time.sleep(0.2)
    raise AssertionError("Valid trace, gauge and derived operation did not reach the captures")


def verify_native(collector, proxy, config, sink, error, prefix):
    name, network = prefix + "-collector", prefix + "-net"
    docker("network", "create", network)
    try:
        with container(name, "--network", network, "--volume", f"{config}:/conf:ro",
                       "--volume", f"{sink}:/sink", "--entrypoint", collector["command"][0],
                       collector["image"], *collector["args"]):
            address = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Networks"][network]["IPAddress"]
            with container(prefix + "-proxy", *proxy_arguments(proxy, config, "container:" + name)) as proxy_name:
                ready(address, 13133, "/", 200)
                ready(address, PROXY_PORT, "/healthz", 204)
                # From outside the Pod-like namespace, neither native transport can be opened.
                for port in (4317, 4318, 4319, 8888, 14250, 8082):
                    try:
                        with socket.create_connection((address, port), timeout=2):
                            raise AssertionError(f"Direct native listener {port} is reachable")
                    except ConnectionRefusedError:
                        pass

                trace, denied, fault, oversized, metrics = payloads()
                headers = {"Content-Type": PROTOBUF}

                def submit(path, body, custom=headers, method=None):
                    return request(address, PROXY_PORT, path, body, custom, method)

                status, body, _ = submit("/v1/traces", trace)
                require(status == 200 and body in (b"", b"\x0a\x00"),
                        f"Valid trace lost its native success response: {status} {body[:160]!r}")
                gzip_headers = {**headers, "Content-Encoding": "gzip"}
                status, body, _ = submit("/v1/metrics", gzip.compress(metrics), gzip_headers)
                require(status == 200 and body in (b"", b"\x0a\x00"),
                        f"Valid gzip gauge lost its native success response: {status} {body[:160]!r}")
                require(submit("/v1/traces", denied)[0] == 200, "Synthetic filter drop did not return normally")
                expect_error(submit("/v1/traces", MARKER.encode(), {"Content-Type": "application/json"}), 415, error)
                expect_error(submit("/v1/traces", b"\x0a\xff" + MARKER.encode()), 400, error)
                expect_error(submit("/v1/traces", fault), 503, error)
                expect_error(submit("/v1/logs", trace), 404, error)
                expect_error(submit("/v1/traces?input=" + MARKER, trace), 404, error)
                expect_error(submit("/v1/%74races", trace), 404, error)
                expect_error(submit("/v1/metrics", metrics, method="PUT"), 405, error)
                expect_error(submit("/v1/traces", oversized), 413, error)
                compact = gzip.compress(oversized)
                require(len(compact) < BODY_LIMIT, "gzip fixture did not reach the native decoded limit")
                decoded_limit = submit("/v1/traces", compact, gzip_headers)
                require(decoded_limit[0] in (400, 413), "Oversized decoded body was accepted")
                expect_error(decoded_limit, decoded_limit[0], error)
                verify_captures(sink)
                for component in (name, proxy_name):
                    require(MARKER not in docker("logs", component), "Request input reached runtime diagnostics")
        print("Verified pinned Collector and proxy, direct-port denial, valid OTLP, gzip limits and fixed errors")
    finally:
        docker("network", "rm", network, check=False)


def verify_retry(proxy, config, error, prefix):
    failures = deque([(429, "12"), (502, None), (503, "25"), (504, MARKER)])
    forwarded_headers = []

    class Backend(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            forwarded_headers.append(dict(self.headers))
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            status, retry_after = failures.popleft()
            self.send_response(status)
            if retry_after is not None:
                self.send_header("Retry-After", retry_after)
            self.send_header("Location", f"https://example.invalid/{MARKER}")
            self.end_headers()
            self.wfile.write(MARKER.encode())

    with ThreadingHTTPServer(("127.0.0.1", 4318), Backend) as backend:
        thread = Thread(target=backend.serve_forever)
        thread.start()
        try:
            with container(prefix + "-retry", *proxy_arguments(proxy, config, "host")):
                ready("127.0.0.1", PROXY_PORT, "/healthz", 204)
                for status, retry_after in list(failures):
                    result = request("127.0.0.1", PROXY_PORT, "/v1/traces", b"\x00", {
                        "Content-Type": PROTOBUF, "Authorization": "Bearer " + MARKER, "X-Probe": MARKER,
                    })
                    expect_error(result, status, error)
                    headers = result[2]
                    expected = retry_after if retry_after and retry_after.isdecimal() else None
                    require(headers.get("Retry-After") == expected and "Location" not in headers,
                            f"Unexpected upstream headers for {status}")
                require(len(forwarded_headers) == 4 and all(MARKER not in str(h) for h in forwarded_headers),
                        "Caller headers reached the native receiver")
        finally:
            backend.shutdown()
            thread.join(timeout=5)
    print("Verified retryable status codes and bounded Retry-After without reflected bodies or caller headers")


def main():
    docs = render_private()
    manifest = docs[("ConfigMap", NAME)]
    collector, proxy = docs[("Deployment", NAME)]["spec"]["template"]["spec"]["containers"]
    prefix = "private-otlp-" + uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix="signoz-private-otlp-") as directory:
        config, sink = Path(directory) / "conf", Path(directory) / "sink"
        error = isolated_config(manifest, config, sink)
        verify_native(collector, proxy, config, sink, error, prefix)
        verify_retry(proxy, config, error, prefix)


if __name__ == "__main__":
    main()
