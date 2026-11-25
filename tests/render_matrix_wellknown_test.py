#!/usr/bin/env python3
"""Render guard: the homeserver must publish its own delegation.

Without `/.well-known/matrix/server`, federation discovery falls back to port
8448. Nothing listens there, so lk-jwt-service's OpenID validation over
`matrix://` fails with "connection refused" and every LiveKit token request
answers 401 — with no component logging a denial, because none of them denied
anything (ORISO-ElementCall#35, ORISO-Livekit#20).
"""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOMAIN = "public.oriso.internal"
MATRIX_IDENTITY = "matrix-identity.oriso.internal"
SERVER_PATH = "/.well-known/matrix/server"


def render(*extra_args: str) -> list[dict]:
    result = subprocess.run(
        [
            "helm", "template", "matrix-wellknown", CHART_DIR,
            "-f", os.path.join(CHART_DIR, "values.yaml.default"),
            "-f", os.path.join(CHART_DIR, "tests", "fixtures", "values-render-domain.yaml"),
            "-f", os.path.join(CHART_DIR, "secrets.yaml.default"),
            "-f", os.path.join(CHART_DIR, "tests", "fixtures", "render-required-secrets.yaml"),
            "--set-string", "global.secrets.redisdefaultPass=test-redis-password",
            "--set", f"global.domainName={DOMAIN}",
            "--set-string", f"matrix.matrixServerName={MATRIX_IDENTITY}",
            "--set-string", f"global.matrix.matrixServerName={MATRIX_IDENTITY}",
            *extra_args,
            "--set-string", "userService.smtpUser=smtp-canary-user",
            "--set-string", "userService.smtpPassword=smtp-canary-password",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise SystemExit("helm template failed")
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def extract_location_block(snippet: str, path: str) -> str:
    """Return the body of the active `location = <path>` block, or "".

    Lines commented out with `#` are ignored, so a disabled handler cannot
    satisfy the assertions below.
    """
    lines = [line for line in snippet.splitlines() if not line.strip().startswith("#")]
    opener = f"location = {path}"
    for index, line in enumerate(lines):
        if opener not in line:
            continue
        depth = 0
        body: list[str] = []
        for current in lines[index:]:
            depth += current.count("{") - current.count("}")
            body.append(current)
            if depth == 0 and body:
                return "\n".join(body)
    return ""


def by_key(documents: list[dict]) -> dict[tuple[str, str], dict]:
    return {(doc["kind"], doc["metadata"]["name"]): doc for doc in documents}


def main() -> None:
    staged = by_key(render())
    switched = by_key(render("--set", "matrix.routeServerDiscoveryViaSynapse=true"))
    ingress_key = ("Ingress", "matrix-client-ingress")
    legacy, active = staged[ingress_key], switched[ingress_key]
    snippet = legacy["metadata"]["annotations"].get("nginx.ingress.kubernetes.io/server-snippet", "")
    block = extract_location_block(snippet, SERVER_PATH)
    assert block, "preparation must retain the existing ingress response"
    assert "return 200" in block
    assert "default_type application/json" in block
    assert "Access-Control-Allow-Origin *" in block
    assert f'"m.server": "{DOMAIN}:443"' in block and "8448" not in block

    assert not any(key.endswith("-snippet") for key in active["metadata"]["annotations"]), (
        "activation must remove the ingress server snippet"
    )
    legacy_paths = legacy["spec"]["rules"][0]["http"]["paths"]
    active_paths = active["spec"]["rules"][0]["http"]["paths"]
    assert active_paths == [*legacy_paths, {
        "backend": {"service": {"name": "matrix-synapse", "port": {"name": "discovery"}}},
        "path": SERVER_PATH, "pathType": "Exact",
    }]
    assert staged[("Ingress", "main-ingress")] == switched[("Ingress", "main-ingress")]

    config_key = ("ConfigMap", "matrix-homeserver-oidc")
    deployment_key = ("Deployment", "matrix-synapse")
    assert staged[config_key] == switched[config_key]
    assert staged[deployment_key] == switched[deployment_key], "activation must not restart Synapse"
    config = staged[config_key]["data"]
    homeserver = yaml.safe_load(config["homeserver.yaml"])
    assert homeserver["server_name"] == MATRIX_IDENTITY
    client_listener = next(item for item in homeserver["listeners"] if item["port"] == 8008)
    listener = next(item for item in homeserver["listeners"] if item["port"] == 8802)
    assert "additional_resources" not in client_listener
    assert listener["resources"] == []
    assert listener["additional_resources"] == {SERVER_PATH: {
        "module": "oriso_server_discovery.ServerDiscoveryResource",
        "config": {"delegated_server": f"{DOMAIN}:443"},
    }}
    assert config["oriso_server_discovery.py"].strip()
    pod = staged[deployment_key]["spec"]["template"]["spec"]
    module = next(volume for volume in pod["volumes"] if volume["name"] == "server-discovery-module")
    assert module["configMap"] == {
        "name": "matrix-homeserver-oidc",
        "items": [{"key": "oriso_server_discovery.py", "path": "oriso_server_discovery.py"}],
    }
    service_key = ("Service", "matrix-synapse")
    assert staged[service_key] == switched[service_key]
    service_ports = staged[service_key]["spec"]["ports"]
    assert {"name": "discovery", "port": 8802, "targetPort": "discovery", "protocol": "TCP"} in service_ports
    container = pod["containers"][0]
    assert {"name": "discovery", "containerPort": 8802, "protocol": "TCP"} in container["ports"]
    module_paths = next(env["value"] for env in container["env"] if env["name"] == "PYTHONPATH").split(":")
    assert "/opt/oriso-server-discovery" in module_paths
    assert {"name": "server-discovery-module", "mountPath": "/opt/oriso-server-discovery", "readOnly": True} in container["volumeMounts"]
    print("PASS: stage Matrix server discovery and switch only the exact ingress route")


if __name__ == "__main__":
    main()
