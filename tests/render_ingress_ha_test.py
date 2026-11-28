#!/usr/bin/env python3
"""Render guard for the bundled ingress controller's availability (ORISO-Helm#544).

The chart's ingress-nginx controller runs two replicas by default, spread over
nodes by soft anti-affinity, with a PodDisruptionBudget that lets a node drain
proceed. A readiness probe keeps a new replica out of the Service until nginx
has loaded its backends, and a shutdown grace period keeps a terminating one
serving while traffic moves away. ip-anonymizer can scale only with a shared
session key (ORISO-IpAnonymizer#8): without one the render fails above one
replica. It gets the same soft spread and drain-safe PDB.
"""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROLLER = {
    "app.kubernetes.io/name": "ingress-nginx",
    "app.kubernetes.io/component": "controller",
    "app.kubernetes.io/instance": "ingress-nginx",
}


def helm(*overrides: str) -> subprocess.CompletedProcess:
    command = ["helm", "template", "caritas", CHART_DIR]
    for path in ("values.yaml.default", "tests/fixtures/values-render-domain.yaml",
                 "secrets.yaml.default", "tests/fixtures/render-required-secrets.yaml"):
        command += ["-f", os.path.join(CHART_DIR, path)]
    for override in overrides:
        command += ["--set", override]
    return subprocess.run(command, capture_output=True, text=True)


def render(*overrides: str) -> dict:
    result = helm(*overrides)
    assert result.returncode == 0, result.stderr
    return {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(result.stdout) if d}


def assert_namespaced_controller() -> None:
    scope = "nginxController.scopeToReleaseNamespace=true"
    both = "nginxController.keepBothRbacScopes=true"
    deployment = ("Deployment", "ingress-nginx-controller")
    old_account = ("ServiceAccount", "ingress-nginx")
    old_binding = ("ClusterRoleBinding", "ingress-nginx")
    scoped_account = ("ServiceAccount", "ingress-nginx-scoped")
    scoped_role = ("Role", "ingress-nginx-scoped")
    scoped_binding = ("RoleBinding", "ingress-nginx-scoped")
    cluster_role = ("ClusterRole", "ingress-nginx-scoped-default")
    cluster_binding = ("ClusterRoleBinding", "ingress-nginx-scoped-default")
    flag = "--watch-namespace=$(POD_NAMESPACE)"

    for overrides, scoped, staged in (
        ((), False, False),
        ((both,), False, True),
        ((scope, both), True, True),
        ((scope,), True, False),
    ):
        documents = render(*overrides)
        controller = documents[deployment]
        pod = controller["spec"]["template"]["spec"]
        annotations = controller["metadata"].get("annotations", {})
        assert (annotations.get("oriso.org/keep-both-ingress-rbac-scopes") == "true") == staged
        assert (flag in pod["containers"][0]["args"]) == scoped
        assert pod["serviceAccountName"] == (scoped_account if scoped else old_account)[1]
        assert (old_account in documents) == (not scoped or staged)
        assert (old_binding in documents) == (not scoped or staged)
        scoped_resources = (scoped_account, scoped_role, scoped_binding, cluster_role, cluster_binding)
        assert all((resource in documents) == (scoped or staged) for resource in scoped_resources)
        if not (scoped or staged):
            continue
        assert documents[cluster_role]["rules"] == [
            {
                "apiGroups": ["networking.k8s.io"],
                "resources": ["ingressclasses"],
                "verbs": ["get", "list", "watch"],
            },
            {
                "apiGroups": [""],
                "resources": ["namespaces"],
                "resourceNames": ["default"],
                "verbs": ["get"],
            },
        ]
        for binding, role in ((scoped_binding, scoped_role), (cluster_binding, cluster_role)):
            assert documents[binding]["subjects"] == [
                {"kind": "ServiceAccount", "name": scoped_account[1], "namespace": "default"}
            ]
            assert documents[binding]["roleRef"] == {
                "apiGroup": "rbac.authorization.k8s.io", "kind": role[0], "name": role[1]
            }
        namespaced = documents[scoped_role]["rules"]
        assert any("secrets" in rule["resources"] for rule in namespaced)
        assert not any("*" in rule["resources"] + rule["verbs"] for rule in namespaced)

    disabled = render(scope, both, "nginxController.enabled=false")
    assert not any(resource in disabled for resource in (old_account, old_binding, *scoped_resources))


def main() -> None:
    assert_namespaced_controller()
    docs = render()
    controller = docs[("Deployment", "ingress-nginx-controller")]
    assert controller["spec"]["replicas"] == 2, controller["spec"]["replicas"]
    # Default RollingUpdate (25%) gives maxUnavailable 0 / maxSurge 1 at two replicas: no gap on rollout.
    assert "strategy" not in controller["spec"], controller["spec"]["strategy"]

    [container] = controller["spec"]["template"]["spec"]["containers"]
    # Without readiness a new replica takes traffic before its backends load and answers 503.
    for probe in ("readinessProbe", "livenessProbe"):
        assert container[probe]["httpGet"] == {"path": "/healthz", "port": 10254}, container.get(probe)
    assert "--shutdown-grace-period=10" in container["args"], container["args"]

    affinity = controller["spec"]["template"]["spec"]["affinity"]["podAntiAffinity"]
    assert "requiredDuringSchedulingIgnoredDuringExecution" not in affinity, "hard anti-affinity breaks single-node installs"
    [term] = affinity["preferredDuringSchedulingIgnoredDuringExecution"]
    assert term["podAffinityTerm"]["topologyKey"] == "kubernetes.io/hostname"
    assert term["podAffinityTerm"]["labelSelector"]["matchLabels"] == CONTROLLER
    assert controller["spec"]["template"]["metadata"]["labels"] == CONTROLLER

    pdb = docs[("PodDisruptionBudget", "ingress-nginx-controller")]
    assert pdb["spec"] == {"maxUnavailable": 1, "selector": {"matchLabels": CONTROLLER}}, pdb["spec"]

    # An operator can still choose one replica; the PDB then does not block a drain.
    single = render("nginxController.replicas=1")
    assert single[("Deployment", "ingress-nginx-controller")]["spec"]["replicas"] == 1
    assert single[("PodDisruptionBudget", "ingress-nginx-controller")]["spec"]["maxUnavailable"] == 1

    disabled = render("nginxController.enabled=false")
    assert ("PodDisruptionBudget", "ingress-nginx-controller") not in disabled
    assert ("Deployment", "ingress-nginx-controller") not in disabled

    anonymizer = render("ipAnonymizer.enabled=true")[("Deployment", "ip-anonymizer")]
    assert anonymizer["spec"]["replicas"] == 1
    [legacy_container] = anonymizer["spec"]["template"]["spec"]["containers"]
    assert not any(var["name"] == "ANON_SESSION_KEY" for var in legacy_container.get("env", []))

    keyed = render(
        "ipAnonymizer.enabled=true",
        "ipAnonymizer.sessionKeySecretName=ip-anonymizer-session-key",
    )[("Deployment", "ip-anonymizer")]
    [keyed_container] = keyed["spec"]["template"]["spec"]["containers"]
    assert [var for var in keyed_container.get("env", []) if var["name"] == "ANON_SESSION_KEY"] == [{
        "name": "ANON_SESSION_KEY",
        "valueFrom": {"secretKeyRef": {"name": "ip-anonymizer-session-key", "key": "ANON_SESSION_KEY"}},
    }], "expected ANON_SESSION_KEY from the configured Secret"
    scaled_docs = render(
        "ipAnonymizer.enabled=true",
        "ipAnonymizer.sessionKeySecretName=ip-anonymizer-session-key",
        "ipAnonymizer.replicas=2",
    )
    scaled = scaled_docs[("Deployment", "ip-anonymizer")]
    assert scaled["spec"]["replicas"] == 2
    assert scaled["spec"]["template"]["spec"]["affinity"] == {"podAntiAffinity": {
        "preferredDuringSchedulingIgnoredDuringExecution": [{"weight": 100, "podAffinityTerm": {
            "labelSelector": {"matchLabels": {"app": "ip-anonymizer"}},
            "topologyKey": "kubernetes.io/hostname"}}]}}, scaled["spec"]["template"]["spec"].get("affinity")
    anon_pdb = scaled_docs[("PodDisruptionBudget", "ip-anonymizer")]
    assert anon_pdb["spec"] == {"maxUnavailable": 1, "selector": {"matchLabels": {"app": "ip-anonymizer"}}}
    assert ("PodDisruptionBudget", "ip-anonymizer") in render("ipAnonymizer.enabled=true")
    assert ("PodDisruptionBudget", "ip-anonymizer") not in render()
    keyless = helm("ipAnonymizer.enabled=true", "ipAnonymizer.replicas=2")
    assert keyless.returncode != 0 and "needs ipAnonymizer.sessionKey or ipAnonymizer.sessionKeySecretName" in keyless.stderr, keyless.stderr
    # The guard only applies when ip-anonymizer is rendered.
    render("ipAnonymizer.replicas=2")

    # Declarative key: secrets.yaml's ipAnonymizer.sessionKey renders the Secret itself.
    key = base64.b64encode(bytes(range(32))).decode()
    declared = render("ipAnonymizer.enabled=true", "ipAnonymizer.replicas=2", f"ipAnonymizer.sessionKey={key}")
    secret = declared[("Secret", "ip-anonymizer-session-key")]
    assert base64.b64decode(secret["data"]["ANON_SESSION_KEY"]).decode() == key
    pod = declared[("Deployment", "ip-anonymizer")]["spec"]["template"]
    [declared_container] = pod["spec"]["containers"]
    assert {"name": "ANON_SESSION_KEY", "valueFrom": {"secretKeyRef": {
        "name": "ip-anonymizer-session-key", "key": "ANON_SESSION_KEY"}}} in declared_container["env"]
    assert pod["metadata"]["annotations"]["checksum/session-key"] == hashlib.sha256(key.encode()).hexdigest()
    assert ("Secret", "ip-anonymizer-session-key") not in render("ipAnonymizer.enabled=true")
    assert ("Secret", "ip-anonymizer-session-key") not in render(f"ipAnonymizer.sessionKey={key}")
    short_key = base64.b64encode(bytes(16)).decode()
    for bad in ("not-base64!", short_key, key.rstrip("=")):
        result = helm("ipAnonymizer.enabled=true", f"ipAnonymizer.sessionKey={bad}")
        assert result.returncode != 0 and "standard-base64 32-byte key" in result.stderr, (bad, result.stderr)
    placeholder = helm("ipAnonymizer.enabled=true", "ipAnonymizer.sessionKey=changeme")
    assert placeholder.returncode != 0, "the shipped placeholder must not render"
    both = helm("ipAnonymizer.enabled=true", f"ipAnonymizer.sessionKey={key}",
                "ipAnonymizer.sessionKeySecretName=ip-anonymizer-session-key")
    assert both.returncode != 0 and "not both" in both.stderr, both.stderr

    print("PASS: ingress controller renders 2 spread, probed replicas with a drain-safe PDB; "
          "ip-anonymizer scales only with a shared, validated session key")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, ValueError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
