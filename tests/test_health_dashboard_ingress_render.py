from __future__ import annotations

import os
import subprocess

import yaml


CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = "oriso"


def render(pre_dev: bool) -> list[dict]:
    command = [
        "helm",
        "template",
        RELEASE,
        CHART_DIR,
        "-f",
        os.path.join(CHART_DIR, "values.yaml.default"),
        "-f",
        os.path.join(CHART_DIR, "secrets.yaml.default"),
    ]
    if pre_dev:
        for setting in (
            "healthDashboard.ingress.enabled=true",
            "healthDashboard.ingress.healthTlsSecretName=health-oriso-site-tls",
            "healthDashboard.ingress.statusAlias.enabled=true",
            "healthDashboard.ingress.statusAlias.tlsSecretName=status-oriso-site-tls",
            "global.domains.health=health.oriso-dev.site",
            "global.domains.status=status.oriso-dev.site",
        ):
            command += ["--set", setting]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
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


def backend_service(ingress: dict) -> tuple[str, int]:
    service = ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    return service["name"], service["port"]["number"]


def test_predev_status_alias_and_health_host_share_the_canonical_dashboard() -> None:
    documents = render(pre_dev=True)
    health = resource(documents, "Ingress", "health-dashboard-ingress")
    status = resource(documents, "Ingress", "status-page-ingress")

    assert health["spec"]["rules"][0]["host"] == "health.oriso-dev.site"
    assert status["spec"]["rules"][0]["host"] == "status.oriso-dev.site"
    assert backend_service(health) == ("oriso-health-dashboard", 9100)
    assert backend_service(status) == ("oriso-health-dashboard", 9100)
    assert health["spec"]["tls"][0] == {
        "hosts": ["health.oriso-dev.site"],
        "secretName": "health-oriso-site-tls",
    }
    assert status["spec"]["tls"][0] == {
        "hosts": ["status.oriso-dev.site"],
        "secretName": "status-oriso-site-tls",
    }

    assert not any(
        document.get("kind") in {"Deployment", "Service"}
        and document.get("metadata", {}).get("name") == "oriso-status-page"
        for document in documents
    )


def test_default_chart_does_not_publish_example_health_hosts() -> None:
    documents = render(pre_dev=False)
    assert not any(
        document.get("kind") == "Ingress"
        and document.get("metadata", {}).get("name")
        in {"health-dashboard-ingress", "status-page-ingress"}
        for document in documents
    )
