#!/usr/bin/env python3
"""OBS-P2: render-based contract for backend OTLP traces/metrics wiring.

Locks the invariants that make the 4 backend services (User/Agency/Tenant/
ConsultingType) actually export traces and metrics to the SigNoz gateway
collector, using the property names verified directly against Spring Boot
4.0.1's real source (not docs — `management.otlp.tracing.endpoint` does NOT
exist in this version; it was renamed to
`management.opentelemetry.tracing.export.otlp.endpoint`, and both OTLP
exporters require the full signal-specific HTTP path, /v1/traces and
/v1/metrics, per the OTel Java SDK's own DEFAULT_ENDPOINT constants):

  - all 4 services get MANAGEMENT_OPENTELEMETRY_TRACING_EXPORT_OTLP_ENDPOINT
    pointing at the gateway collector with the /v1/traces path,
  - all 4 get MANAGEMENT_OTLP_METRICS_EXPORT_URL with the /v1/metrics path,
  - both come with an explicit *_ENABLED toggle (an empty/unset endpoint
    alone does not reliably no-op — the tracing connection-details bean is
    gated by @ConditionalOnProperty with no havingValue, which Spring treats
    as satisfied by an empty string; Micrometer's OTLP metrics registry has
    its own default endpoint fallback),
  - dev/pre-dev enable OTLP export; prod disables it (deferred to OBS-P6,
    the pseudonymisation collector pipeline),
  - the endpoint host resolves to the same gateway service OBS-P1 deployed
    (signoz.otelCollector.fullname), not a hardcoded or divergent name.
"""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SERVICES = [
    ("userservice-configmap-env", "userservice", "userservice"),
    ("agencyservice-configmap-env", "agencyservice", "agencyservice"),
    ("tenantservice-configmap-env", "tenantservice", "tenantservice"),
    ("consultingtypeservice-configmap-env", "consultingtypeservice", "consultingtypeservice"),
]

OTLP_KEYS = (
    "MANAGEMENT_OPENTELEMETRY_TRACING_EXPORT_OTLP_ENDPOINT",
    "MANAGEMENT_TRACING_EXPORT_OTLP_ENABLED",
    "MANAGEMENT_OTLP_METRICS_EXPORT_URL",
    "MANAGEMENT_OTLP_METRICS_EXPORT_ENABLED",
)

WRONG_PROPERTY_ENV_NAMES = (
    "MANAGEMENT_OTLP_TRACING_ENDPOINT",
    "MANAGEMENT_OTLP_TRACING_ENABLED",
)


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def render(*value_files: str, extra_set: list[str] | None = None) -> list[dict]:
    cmd = ["helm", "template", "oriso-platform", CHART_DIR, "--namespace", "caritas"]
    for vf in value_files:
        cmd += ["-f", os.path.join(CHART_DIR, vf)]
    cmd += ["-f", os.path.join(CHART_DIR, "secrets.yaml.default")]
    cmd += ["--set", "global.secrets.clickhousePassword=x"]
    if extra_set:
        for kv in extra_set:
            cmd += ["--set", kv]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"helm template failed for {value_files}: {result.stderr}")
    return list(yaml.safe_load_all(result.stdout))


def find_configmap(docs: list[dict], name: str) -> dict:
    for doc in docs:
        if doc and doc.get("kind") == "ConfigMap" and doc.get("metadata", {}).get("name") == name:
            return doc
    fail(f"ConfigMap {name} not found in render output")
    raise AssertionError  # unreachable, satisfies type checkers


def find_deployment(docs: list[dict], name: str) -> dict:
    for doc in docs:
        if doc and doc.get("kind") == "Deployment" and doc.get("metadata", {}).get("name") == name:
            return doc
    fail(f"Deployment {name} not found in render output")
    raise AssertionError  # unreachable, satisfies type checkers


def check_deployment_wires_configmap_keys(deployment: dict, configmap_name: str, service_label: str) -> None:
    # This chart wires env vars per-key via configMapKeyRef, NOT a blanket
    # envFrom — a key existing in the ConfigMap's data does nothing on the
    # pod unless the Deployment's container also lists it under `env:`.
    # (Found live: OBS-P2's first deploy pass added the 4 keys to the
    # ConfigMap only, and none of them reached a running pod.)
    containers = deployment["spec"]["template"]["spec"]["containers"]
    env_list = containers[0].get("env", [])
    wired = {
        e["name"]
        for e in env_list
        if e.get("valueFrom", {}).get("configMapKeyRef", {}).get("name") == configmap_name
    }
    missing = [k for k in OTLP_KEYS if k not in wired]
    if missing:
        fail(f"{service_label}: Deployment does not reference {missing} via configMapKeyRef "
             f"(name={configmap_name}) — the ConfigMap having these keys is not enough, the "
             f"container's env: list must also list them")


def check_env(data: dict, service_label: str, expect_enabled: bool) -> None:
    for wrong in WRONG_PROPERTY_ENV_NAMES:
        if wrong in data:
            fail(f"{service_label}: uses non-existent Spring Boot 4.0.1 property env {wrong} "
                 f"(management.otlp.tracing.endpoint was renamed to "
                 f"management.opentelemetry.tracing.export.otlp.endpoint)")

    trace_endpoint = data.get("MANAGEMENT_OPENTELEMETRY_TRACING_EXPORT_OTLP_ENDPOINT")
    metrics_url = data.get("MANAGEMENT_OTLP_METRICS_EXPORT_URL")
    trace_enabled = data.get("MANAGEMENT_TRACING_EXPORT_OTLP_ENABLED")
    metrics_enabled = data.get("MANAGEMENT_OTLP_METRICS_EXPORT_ENABLED")

    if trace_endpoint is None:
        fail(f"{service_label}: missing MANAGEMENT_OPENTELEMETRY_TRACING_EXPORT_OTLP_ENDPOINT")
    if not trace_endpoint.endswith("/v1/traces"):
        fail(f"{service_label}: tracing endpoint must end in /v1/traces (OTel SDK requires the "
             f"full signal-specific path), got {trace_endpoint!r}")
    if "oriso-platform-otel-collector" not in trace_endpoint:
        fail(f"{service_label}: tracing endpoint does not point at the OBS-P1 gateway collector "
             f"service, got {trace_endpoint!r}")

    if metrics_url is None:
        fail(f"{service_label}: missing MANAGEMENT_OTLP_METRICS_EXPORT_URL")
    if not metrics_url.endswith("/v1/metrics"):
        fail(f"{service_label}: metrics url must end in /v1/metrics, got {metrics_url!r}")
    if "oriso-platform-otel-collector" not in metrics_url:
        fail(f"{service_label}: metrics url does not point at the OBS-P1 gateway collector "
             f"service, got {metrics_url!r}")

    expected = "true" if expect_enabled else "false"
    if str(trace_enabled).lower() != expected:
        fail(f"{service_label}: MANAGEMENT_TRACING_EXPORT_OTLP_ENABLED expected {expected!r}, "
             f"got {trace_enabled!r}")
    if str(metrics_enabled).lower() != expected:
        fail(f"{service_label}: MANAGEMENT_OTLP_METRICS_EXPORT_ENABLED expected {expected!r}, "
             f"got {metrics_enabled!r}")


def main() -> None:
    predev_docs = render("values.yaml.default", "values-pre-dev.yaml")
    prod_docs = render("values.yaml.default", "values-prod.yaml")

    for cm_name, deploy_name, label in SERVICES:
        predev_cm = find_configmap(predev_docs, cm_name)
        check_env(predev_cm["data"], f"pre-dev/{label}", expect_enabled=True)

        prod_cm = find_configmap(prod_docs, cm_name)
        check_env(prod_cm["data"], f"prod/{label}", expect_enabled=False)

        predev_deploy = find_deployment(predev_docs, deploy_name)
        check_deployment_wires_configmap_keys(predev_deploy, cm_name, f"pre-dev/{label}")

    print("OK: OBS-P2 contract holds — all 4 backend services wire the real Spring Boot 4.0.1 "
          "OTLP properties (not the non-existent Boot-3-era names) with full /v1/traces and "
          "/v1/metrics paths against the OBS-P1 gateway collector, enabled on pre-dev and "
          "disabled on prod pending OBS-P6.")


if __name__ == "__main__":
    main()
