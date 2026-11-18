#!/usr/bin/env python3
"""Render guard for pod and container securityContext (ORISO-Helm#539).

Every chart workload runs with the restricted Pod Security settings: non-root,
RuntimeDefault seccomp, no privilege escalation, all capabilities dropped and a
read-only root filesystem. The exceptions below are the only ones allowed; each
has its reason next to the setting in its template.
"""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}

# (workload, container) -> capabilities it may add after dropping ALL.
ROOT = {
    ("admin", "admin"): ["CHOWN", "SETUID", "SETGID", "NET_BIND_SERVICE"],
    ("matrix-synapse", "fix-permissions"): ["CHOWN", "FOWNER", "DAC_OVERRIDE"],
}
ADDED_CAPABILITIES = {**ROOT, ("ingress-nginx-controller", "controller"): ["NET_BIND_SERVICE"]}
WRITABLE_ROOT = {
    ("admin", "admin"),
    ("frontend", "frontend"),
    ("keycloak", "keycloak"),
    ("ingress-nginx-controller", "controller"),
    ("create-mongo-users", "wait-for-mongo-ready"),
    ("create-mongo-users", "create-mongo-users"),
    ("keycloak-bootstrap-users", "bootstrap-users"),
    ("keycloak-verify-2fa-contract", "verify-2fa-contract"),
    # Pre-existing, outside ORISO-Helm#539.
    ("ip-anonymizer", "ip-anonymizer"),
    ("media-scanner", "clamav"),
}
API_CLIENTS = {"caritas-health-dashboard", "ingress-nginx-controller", "matrixrtc-bootstrap-token"}
HOST_NETWORK = {"livekit"}
EXPECTED = {
    "admin", "agencyservice", "consultingtypeservice", "frontend", "caritas-health-dashboard",
    "livekit", "matrix-postgres", "ingress-nginx-controller", "storybook-frontend",
    "storybook-admin", "tenantservice", "userservice", "mariadb", "mongodb", "redis", "rabbitmq",
    "keycloak", "matrix-synapse", "element-call",
}


def render() -> list[dict]:
    result = subprocess.run(
        [
            "helm", "template", "caritas", CHART_DIR,
            "-f", os.path.join(CHART_DIR, "values.yaml.default"),
            "-f", os.path.join(CHART_DIR, "tests", "fixtures", "values-render-domain.yaml"),
            "-f", os.path.join(CHART_DIR, "secrets.yaml.default"),
            "-f", os.path.join(CHART_DIR, "tests", "fixtures", "render-required-secrets.yaml"),
            "--set-string", "global.secrets.redisdefaultPass=test-redis-password",
            "--set-string", "userService.smtpUser=smtp-canary-user",
            "--set-string", "userService.smtpPassword=smtp-canary-password",
            "--set", "storybook.enabled=true",
            "--set-string", "storybook.ingress.authSecret=storybook-basic-auth",
            "--set", "mediaScanner.enabled=true",
            "--set-string", "mediaScanner.requestSecret.existingSecret=media-scanner-request",
            "--set", "ipAnonymizer.enabled=true",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def pod_spec(document: dict) -> dict:
    if document["kind"] == "CronJob":
        return document["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return document["spec"]["template"]["spec"]


def check(documents: list[dict]) -> list[str]:
    failures = []
    seen = set()
    for document in documents:
        if document.get("kind") not in WORKLOAD_KINDS:
            continue
        name = document["metadata"]["name"]
        seen.add(name)
        spec = pod_spec(document)
        pod = spec.get("securityContext") or {}
        if spec.get("automountServiceAccountToken") is not False and name not in API_CLIENTS:
            failures.append(f"{name}: automountServiceAccountToken is not false")
        if spec.get("hostNetwork") and name not in HOST_NETWORK:
            failures.append(f"{name}: hostNetwork")
        for container in (spec.get("initContainers") or []) + spec["containers"]:
            key = (name, container["name"])
            own = container.get("securityContext") or {}
            effective = {**pod, **own}
            where = f"{name}/{container['name']}"
            if (effective.get("seccompProfile") or {}).get("type") != "RuntimeDefault":
                failures.append(f"{where}: seccompProfile is not RuntimeDefault")
            if (effective.get("runAsNonRoot") is True) == (key in ROOT):
                failures.append(f"{where}: runAsNonRoot={effective.get('runAsNonRoot')!r}")
            if own.get("allowPrivilegeEscalation") is not False:
                failures.append(f"{where}: allowPrivilegeEscalation is not false")
            capabilities = own.get("capabilities") or {}
            if capabilities.get("drop") != ["ALL"]:
                failures.append(f"{where}: capabilities.drop is not [ALL]")
            if sorted(capabilities.get("add") or []) != sorted(ADDED_CAPABILITIES.get(key, [])):
                failures.append(f"{where}: capabilities.add={capabilities.get('add')!r}")
            if (own.get("readOnlyRootFilesystem") is True) == (key in WRITABLE_ROOT):
                failures.append(f"{where}: readOnlyRootFilesystem={own.get('readOnlyRootFilesystem')!r}")
    failures += [f"{name}: not rendered" for name in sorted(EXPECTED - seen)]
    return failures


def main() -> None:
    failures = check(render())
    assert not failures, "\n".join(failures)

    # The check itself must catch a regression.
    broken = render()
    for document in broken:
        if document.get("kind") == "Deployment" and document["metadata"]["name"] == "redis":
            del pod_spec(document)["containers"][0]["securityContext"]
    assert any(f.startswith("redis/redis:") for f in check(broken)), "check missed a removed securityContext"

    print("PASS: every chart workload renders the restricted securityContext or a listed exception")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
