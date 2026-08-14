#!/usr/bin/env python3
"""Render guard for the fail-closed media scanner (epic ORISO-Admin#366, #283).

Four promises are asserted here, because each of them is easy to break by
accident and expensive to notice in production:

  1. Off by default — an untouched chart deploys nothing and changes no client.
  2. Pinned by digest — nothing in the media path floats on a rebuilt tag.
  3. Secrets by reference — the chart never renders a key out of a values file,
     and refuses to render at all when one is missing.
  4. Fail-closed routing — downloads go to the scanner, the unscanned route is
     closed, and the AI check cannot be switched on without a recorded
     sub-processor agreement.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = [
    "templates/_helpers.tpl",
    "templates/frontend/frontend-configmap.yaml",
    "templates/media-scanner/media-scanner-configmap.yaml",
    "templates/media-scanner/media-scanner-deployment.yaml",
    "templates/media-scanner/media-scanner-service.yaml",
    "templates/media-scanner/media-scanner-ingress.yaml",
    "templates/media-scanner/media-scanner-direct-media-block-ingress.yaml",
]
FIXTURES = os.path.join(CHART_DIR, "tests", "fixtures")
CLAMAV_VALUES = os.path.join(FIXTURES, "values-media-scanner-clamav.yaml")
AI_VALUES = os.path.join(FIXTURES, "values-media-scanner-ai.yaml")


def build_minimal_chart(destination: str) -> None:
    os.makedirs(os.path.join(destination, "templates"))
    with open(os.path.join(destination, "Chart.yaml"), "w", encoding="utf-8") as chart:
        chart.write("apiVersion: v2\nname: media-scanner\nversion: 0.0.0\nappVersion: 0.0.0\n")
    shutil.copyfile(
        os.path.join(CHART_DIR, "values.yaml.default"),
        os.path.join(destination, "values.yaml"),
    )
    for relative_path in TEMPLATES:
        target = os.path.join(destination, relative_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(os.path.join(CHART_DIR, relative_path), target)


def helm_template(chart: str, *values: str) -> subprocess.CompletedProcess:
    if shutil.which("helm") is None:
        raise AssertionError("helm is required to run this guard")
    command = ["helm", "template", "media-scanner", chart]
    for values_file in values:
        command.extend(["-f", values_file])
    return subprocess.run(command, capture_output=True, text=True, check=False)


def render(chart: str, *values: str) -> list[dict]:
    result = helm_template(chart, *values)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def render_error(chart: str, *values: str) -> str:
    result = helm_template(chart, *values)
    if not result.returncode:
        raise AssertionError("expected the render to fail, but it succeeded")
    return result.stderr


def find(documents: list[dict], kind: str, name: str) -> dict | None:
    for document in documents:
        if document.get("kind") == kind and document.get("metadata", {}).get("name") == name:
            return document
    return None


def get(documents: list[dict], kind: str, name: str) -> dict:
    document = find(documents, kind, name)
    assert document is not None, "missing %s/%s" % (kind, name)
    return document


def write_values(directory: str, name: str, body: str) -> str:
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    return path


def assert_off_by_default(chart: str) -> None:
    documents = render(chart)

    for kind, name in [
        ("Deployment", "media-scanner"),
        ("Service", "media-scanner"),
        ("ConfigMap", "media-scanner-config"),
        ("Ingress", "media-scanner-ingress"),
        ("Ingress", "media-scanner-direct-media-block-ingress"),
    ]:
        assert find(documents, kind, name) is None, "%s/%s renders without opt-in" % (kind, name)

    frontend = get(documents, "ConfigMap", "frontend-configmap")
    assert frontend["data"]["VITE_MEDIA_SCANNER_URL"] == "", (
        "clients must not be pointed at a scanner that is not deployed"
    )


def assert_images_pinned(deployment: dict) -> None:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 2, "expected the scanner plus its ClamAV sidecar"
    for container in containers:
        image = container["image"]
        assert "@sha256:" in image, "%s is not pinned by digest: %s" % (container["name"], image)
        assert ":latest" not in image, "%s floats on latest: %s" % (container["name"], image)


def assert_secrets_by_reference(documents: list[dict], deployment: dict) -> None:
    # The chart must never materialise a secret of its own from values.
    for document in documents:
        assert document.get("kind") != "Secret", "the chart must not render secrets itself"

    volumes = {volume["name"]: volume for volume in deployment["spec"]["template"]["spec"]["volumes"]}
    assert volumes["request-secret"]["secret"]["secretName"] == "media-scanner-request-secret"


def assert_clamav_only(chart: str) -> None:
    documents = render(chart, CLAMAV_VALUES)

    deployment = get(documents, "Deployment", "media-scanner")
    assert_images_pinned(deployment)
    assert_secrets_by_reference(documents, deployment)

    config_map = get(documents, "ConfigMap", "media-scanner-config")
    config = yaml.safe_load(config_map["data"]["config.yaml"])
    # Schema of the pinned release, not of an older one.
    assert config["web"]["port"] == 8080, "config must use the `web` section, not the retired `server`"
    assert config["crypto"]["request_secret_path"] == "/crypto/request-secret"
    assert config["scan"]["script"] == "/scripts/scan.py"
    assert config["download"]["base_homeserver_url"] == "http://matrix-synapse:8008"
    # A check that could not run must never be remembered as a verdict.
    assert config["result_cache"]["exit_codes_to_ignore"] == [2]

    scan_script = config_map["data"]["scan.py"]
    assert "zINSTREAM" in scan_script, "the scan must actually talk to clamd"
    assert "ai-check.py" not in scan_script, "the AI check must not run while it is switched off"

    # Downloads reach the scanner, not Synapse.
    ingress = get(documents, "Ingress", "media-scanner-ingress")
    paths = ingress["spec"]["rules"][0]["http"]["paths"]
    assert [path["path"] for path in paths] == ["/_matrix/media_proxy"]
    assert paths[0]["backend"]["service"]["name"] == "media-scanner"

    # And the unscanned route is closed by default.
    blocker = get(documents, "Ingress", "media-scanner-direct-media-block-ingress")
    blocked = [path["path"] for path in blocker["spec"]["rules"][0]["http"]["paths"]]
    assert "/_matrix/media/v3/download" in blocked
    assert "/_matrix/media/v3/thumbnail" in blocked
    assert "/_matrix/client/v1/media/download" in blocked
    assert not any("upload" in path for path in blocked), (
        "uploads must stay reachable, otherwise the scanner has nothing to fetch"
    )
    snippet = blocker["metadata"]["annotations"]["nginx.ingress.kubernetes.io/configuration-snippet"]
    assert "return 403" in snippet

    frontend = get(documents, "ConfigMap", "frontend-configmap")
    assert (
        frontend["data"]["VITE_MEDIA_SCANNER_URL"]
        == "https://pre-dev.example.org/_matrix/media_proxy/unstable"
    )


def assert_missing_request_secret_refuses(chart: str, scratch: str) -> None:
    values = write_values(
        scratch,
        "no-request-secret.yaml",
        "mediaScanner:\n  enabled: true\n",
    )
    error = render_error(chart, values)
    assert "requestSecret.existingSecret" in error, error


def assert_ai_gate(chart: str, scratch: str) -> None:
    unsigned = write_values(
        scratch,
        "ai-unsigned.yaml",
        "mediaScanner:\n"
        "  enabled: true\n"
        "  requestSecret:\n"
        "    existingSecret: media-scanner-request-secret\n"
        "  aiCheck:\n"
        "    enabled: true\n"
        "    existingSecret: media-scanner-ai-key\n",
    )
    error = render_error(chart, unsigned)
    assert "subProcessorAgreementSigned" in error, error

    keyless = write_values(
        scratch,
        "ai-keyless.yaml",
        "mediaScanner:\n"
        "  enabled: true\n"
        "  requestSecret:\n"
        "    existingSecret: media-scanner-request-secret\n"
        "  aiCheck:\n"
        "    enabled: true\n"
        "    subProcessorAgreementSigned: true\n",
    )
    error = render_error(chart, keyless)
    assert "aiCheck.existingSecret" in error, error

    documents = render(chart, AI_VALUES)
    config_map = get(documents, "ConfigMap", "media-scanner-config")
    assert "ai-check.py" in config_map["data"]["scan.py"], "the AI check must run once it is gated in"

    deployment = get(documents, "Deployment", "media-scanner")
    volumes = {volume["name"]: volume for volume in deployment["spec"]["template"]["spec"]["volumes"]}
    assert volumes["ai-secret"]["secret"]["secretName"] == "media-scanner-ai-key"


def assert_staged_rollout(chart: str, scratch: str) -> None:
    values = write_values(
        scratch,
        "staged.yaml",
        "mediaScanner:\n"
        "  enabled: true\n"
        "  blockDirectMediaAccess: false\n"
        "  requestSecret:\n"
        "    existingSecret: media-scanner-request-secret\n",
    )
    documents = render(chart, values)
    assert find(documents, "Ingress", "media-scanner-ingress") is not None
    assert find(documents, "Ingress", "media-scanner-direct-media-block-ingress") is None


def main() -> None:
    temporary_directory = tempfile.mkdtemp(prefix="media-scanner-render-")
    try:
        chart = os.path.join(temporary_directory, "chart")
        build_minimal_chart(chart)

        assert_off_by_default(chart)
        assert_clamav_only(chart)
        assert_missing_request_secret_refuses(chart, temporary_directory)
        assert_ai_gate(chart, temporary_directory)
        assert_staged_rollout(chart, temporary_directory)
    finally:
        shutil.rmtree(temporary_directory, ignore_errors=True)

    print("media scanner render guard: OK")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as error:
        print("media scanner render guard: FAIL\n%s" % error, file=sys.stderr)
        sys.exit(1)
