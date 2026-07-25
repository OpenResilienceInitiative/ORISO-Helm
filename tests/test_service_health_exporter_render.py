from __future__ import annotations

import os
import subprocess

import yaml


CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = "oriso"


def helm_template(
    enabled: bool, signoz_enabled: bool
) -> subprocess.CompletedProcess[str]:
    command = [
        "helm",
        "template",
        RELEASE,
        CHART_DIR,
        "-f",
        os.path.join(CHART_DIR, "values.yaml.default"),
        "-f",
        os.path.join(CHART_DIR, "secrets.yaml.default"),
        "--set",
        f"serviceHealthExporter.enabled={str(enabled).lower()}",
        "--set",
        f"signoz.enabled={str(signoz_enabled).lower()}",
        "--set",
        "global.observability.environment=pre-dev",
    ]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def render(enabled: bool, signoz_enabled: bool = False) -> list[dict]:
    result = helm_template(enabled, signoz_enabled)
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if isinstance(document, dict)]


def resource(documents: list[dict], kind: str, name: str) -> dict:
    matches = [
        document
        for document in documents
        if document.get("kind") == kind and document.get("metadata", {}).get("name") == name
    ]
    assert matches, f"missing rendered {kind}/{name}"
    return matches[0]


def test_renders_dependency_free_body_validating_health_collector() -> None:
    documents = render(enabled=True, signoz_enabled=True)
    configmap = resource(documents, "ConfigMap", f"{RELEASE}-service-health-exporter")
    deployment = resource(documents, "Deployment", f"{RELEASE}-service-health-exporter")

    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == (
        "otel/opentelemetry-collector-contrib"
        "@sha256:125bdbeb7590cc1952c5b3430ecf14063568980c2c93d5b38676cc0446ed8108"
    )
    assert container["command"] == ["/otelcol-contrib"]
    assert "pip install" not in yaml.safe_dump(deployment)
    assert container["livenessProbe"]["httpGet"]["port"] == "health"
    assert container["readinessProbe"]["httpGet"]["port"] == "health"

    config = yaml.safe_load(configmap["data"]["otel-collector-config.yaml"])
    receiver = config["receivers"]["http_check"]
    assert receiver["collection_interval"] == "10s"
    assert receiver["metrics"]["httpcheck.status"]["enabled"] is True
    assert receiver["metrics"]["httpcheck.error"]["enabled"] is True
    assert receiver["metrics"]["httpcheck.validation.passed"]["enabled"] is True
    assert receiver["metrics"]["httpcheck.validation.failed"]["enabled"] is True

    targets = receiver["targets"]
    assert len(targets) == 4
    assert {target["endpoint"] for target in targets} == {
        "http://oriso-tenantservice:8080/actuator/health",
        "http://oriso-userservice:8080/actuator/health",
        "http://oriso-consultingtypeservice:8080/actuator/health",
        "http://oriso-agencyservice:8080/actuator/health",
    }
    for target in targets:
        assert target["timeout"] == "5s"
        assert {"json_path": "$.status", "equals": "UP"} in target["validations"]

    resource_attributes = config["processors"]["resource/identity"]["attributes"]
    assert {
        "key": "service.name",
        "value": "service-health-exporter",
        "action": "upsert",
    } in resource_attributes
    assert {
        "key": "deployment.environment",
        "value": "pre-dev",
        "action": "upsert",
    } in resource_attributes
    assert config["service"]["pipelines"]["metrics"] == {
        "receivers": ["http_check"],
        "processors": ["memory_limiter", "resource/identity", "batch"],
        "exporters": ["otlp"],
    }

    contract = yaml.safe_load(configmap["data"]["service-health-contract.yaml"])
    assert contract["groupBy"] == ["http.url", "deployment.environment"]
    assert contract["up"]["requiresFresh"] == [
        "httpcheck.status == 1",
        "httpcheck.validation.passed >= 1",
    ]
    assert contract["down"]["anyFresh"] == [
        "httpcheck.status != 1",
        "httpcheck.error > 0",
        "httpcheck.validation.failed > 0",
    ]
    assert contract["down"]["missingOrStale"] == [
        "httpcheck.status",
        "httpcheck.validation.passed",
    ]


def test_rejects_exporter_without_its_signoz_destination() -> None:
    result = helm_template(enabled=True, signoz_enabled=False)

    assert result.returncode != 0
    assert "serviceHealthExporter.enabled requires signoz.enabled=true" in result.stderr


def test_does_not_render_when_disabled() -> None:
    documents = render(enabled=False)
    assert not any(
        document.get("metadata", {}).get("name") == f"{RELEASE}-service-health-exporter"
        for document in documents
    )
