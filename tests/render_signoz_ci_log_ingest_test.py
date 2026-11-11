#!/usr/bin/env python3
"""Render the operator-only Dev receiver and keep it outside Kubernetes Services."""

import yaml

from render_signoz_dev_observability_test import render_dev_fails, render_dev_with


COLLECTOR = "caritas-signoz-otel-collector"
OPT_IN = {"otelCollector": {"ciLogIngest": {"enabled": True}}}


def collector_config(docs):
    return yaml.safe_load(
        docs["ConfigMap", COLLECTOR]["data"]["otel-collector-config.yaml"]
    )


def main():
    baseline = render_dev_with({})
    disabled = collector_config(baseline)
    assert "otlp/ci-results" not in disabled["receivers"]
    assert "logs/ci-results" not in disabled["service"]["pipelines"]
    logs = disabled["service"]["pipelines"]["logs"]
    assert logs["receivers"] == ["otlp"]
    assert logs["processors"] == ["memory_limiter", "filter/reserved-ci-logs", "batch"]

    enabled = render_dev_with({"signoz": OPT_IN})
    config = collector_config(enabled)
    assert config["receivers"]["otlp/ci-results"]["protocols"] == {
        "http": {"endpoint": "127.0.0.1:4331", "max_request_body_size": 262144},
    }
    pipelines = config["service"]["pipelines"]
    assert pipelines["logs/ci-results"] == {
        "receivers": ["otlp/ci-results"],
        "processors": ["memory_limiter", "filter/ci-results", "transform/ci-results", "batch"],
        "exporters": logs["exporters"],
    }
    assert {name: pipeline for name, pipeline in pipelines.items() if name != "logs/ci-results"} == (
        disabled["service"]["pipelines"]
    )
    assert {key: doc for key, doc in enabled.items() if key[0] in ("Service", "Ingress")} == {
        key: doc for key, doc in baseline.items() if key[0] in ("Service", "Ingress")
    }
    pod = enabled["Deployment", COLLECTOR]["spec"]["template"]["spec"]
    assert not pod.get("hostNetwork")
    collector = next(container for container in pod["containers"] if container["name"] == "collector")
    assert all(port["containerPort"] != 4331 and "hostPort" not in port for port in collector["ports"])
    assert "--config=/conf/otel-collector-config.yaml" in collector["args"]
    assert any(volume.get("configMap", {}).get("name") == COLLECTOR for volume in pod["volumes"])

    for global_values in (
        {"domainName": "elsewhere.oriso.org"},
        {"observability": {"deploymentEnvironment": "pre-dev"}},
    ):
        error = render_dev_fails({"signoz": OPT_IN, "global": global_values})
        assert "ciLogIngest.enabled=true requires deploymentEnvironment=dev and domainName=dev.oriso.org" in error
    error = render_dev_fails({"signoz": {"enabled": False, **OPT_IN}})
    assert "ciLogIngest.enabled=true requires signoz.enabled=true" in error
    error = render_dev_fails({"signoz": {"otelCollector": {
        "ciLogIngest": {"enabled": True},
        "configMap": {"create": False},
    }}})
    assert "ciLogIngest.enabled=true requires signoz.otelCollector.configMap.create=true" in error
    error = render_dev_fails({"signoz": {"otelCollector": {"ciLogIngest": {"enabled": "false"}}}})
    assert "ciLogIngest.enabled must be a boolean" in error
    print("PASS: CI log receiver is off by default, Dev-only, Pod-loopback and logs-only")


if __name__ == "__main__":
    main()
