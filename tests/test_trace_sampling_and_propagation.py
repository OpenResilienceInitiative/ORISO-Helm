"""The backend tracing knobs that decide whether a test run is traceable.

Two defects were measured against PreDev on 2026-07-26:

* Sampling was configured nowhere, so Spring Boot's default of 0.1 applied and
  nine out of ten traces were discarded.
* 35 probe requests carrying a sampled W3C ``traceparent`` produced zero spans
  under the supplied trace ID (ClickHouse ingestion lag measured at 7s, so not
  a timing artefact).

Both knobs are therefore rendered explicitly for every backend, and the
per-environment sampling rate has to stay overridable.
"""

from __future__ import annotations

import os
import subprocess

import yaml


CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = "oriso-platform"
BACKENDS = ("userservice", "agencyservice", "tenantservice", "consultingtypeservice")


def render(**overrides: str) -> list[dict]:
    command = [
        "helm",
        "template",
        RELEASE,
        CHART_DIR,
        "--namespace",
        "caritas",
        "-f",
        os.path.join(CHART_DIR, "values.yaml.default"),
        "-f",
        os.path.join(CHART_DIR, "secrets.yaml.default"),
    ]
    for key, value in overrides.items():
        command += ["--set", f"{key}={value}"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return [d for d in yaml.safe_load_all(result.stdout) if isinstance(d, dict)]


def config_map(documents: list[dict], name: str) -> dict:
    for document in documents:
        if document.get("kind") == "ConfigMap" and document["metadata"]["name"] == name:
            return document
    raise AssertionError(f"missing ConfigMap/{name}")


def container_env(documents: list[dict], workload_name: str) -> list[dict]:
    for document in documents:
        if document.get("kind") in {"Deployment", "StatefulSet"} and (
            document["metadata"]["name"] == workload_name
        ):
            return document["spec"]["template"]["spec"]["containers"][0].get("env", [])
    raise AssertionError(f"missing workload {workload_name}")


def test_every_backend_declares_sampling_and_propagation() -> None:
    documents = render()
    for backend in BACKENDS:
        data = config_map(documents, f"{backend}-configmap-env")["data"]
        assert data["MANAGEMENT_TRACING_PROPAGATION_TYPE"] == "W3C", backend
        assert data["MANAGEMENT_TRACING_SAMPLING_PROBABILITY"] == "0.1", backend


def test_the_keys_reach_the_container_not_just_the_config_map() -> None:
    # A ConfigMap entry nothing references changes no runtime behaviour.
    documents = render()
    for backend in BACKENDS:
        names = {entry["name"] for entry in container_env(documents, backend)}
        assert "MANAGEMENT_TRACING_SAMPLING_PROBABILITY" in names, backend
        assert "MANAGEMENT_TRACING_PROPAGATION_TYPE" in names, backend


def test_sampling_is_overridable_per_environment() -> None:
    # PreDev raises this to 1.0 so an E2E run has a trace for every request.
    documents = render(**{"global.observability.tracingSamplingProbability": "1.0"})
    for backend in BACKENDS:
        data = config_map(documents, f"{backend}-configmap-env")["data"]
        assert data["MANAGEMENT_TRACING_SAMPLING_PROBABILITY"] == "1.0", backend


def test_production_default_is_unchanged_by_this_chart() -> None:
    # Full sampling in production would multiply trace volume tenfold; the
    # chart default deliberately matches Spring Boot's own default.
    documents = render()
    data = config_map(documents, "userservice-configmap-env")["data"]
    assert data["MANAGEMENT_TRACING_SAMPLING_PROBABILITY"] == "0.1"
