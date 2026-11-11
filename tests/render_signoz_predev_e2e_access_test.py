#!/usr/bin/env python3
"""Render the two restricted Kubernetes identities used by the protected E2E workflow."""

from render_signoz_dev_observability_test import by_key, render


PRIVATE = "oriso-predev-j08"
HOSTED = "oriso-predev-e2e-report"
PREDEV = ("values-dev.yaml", "values-pre-dev.yaml")
FLAG = "signoz.orisoPreDevE2EAccess.enabled"
ENABLED = ("--set", f"{FLAG}=true")
KINDS = {"ServiceAccount", "Role", "RoleBinding"}
ACCESS_KEYS = {(kind, name) for kind in KINDS for name in (PRIVATE, HOSTED)}
COMMON_DEPLOYMENTS = ["frontend", "userservice", "matrix-synapse"]
MATRIXRTC_DEPLOYMENTS = ["element-call", "matrixrtc-auth-policy-gateway", "matrixrtc-authorization-service", "livekit"]


def rule(group, resource, verb, names=None):
    entry = {"apiGroups": [group], "resources": [resource], "verbs": [verb]}
    if names is not None:
        entry["resourceNames"] = names
    return entry


def assert_identity(docs, name, deployments, extra_rules):
    service_account = docs[("ServiceAccount", name)]
    assert service_account["automountServiceAccountToken"] is False
    assert "secrets" not in service_account and "imagePullSecrets" not in service_account
    role = docs[("Role", name)]
    assert role["rules"] == [
        rule("apps", "deployments", "get", deployments),
        rule("apps", "statefulsets", "get", ["caritas-signoz"]),
        rule("apps", "replicasets", "get"),
        rule("", "pods", "list"),
        *extra_rules,
    ]
    binding = docs[("RoleBinding", name)]
    assert binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name}
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": name, "namespace": "caritas"}]
    for kind in KINDS:
        assert docs[(kind, name)]["metadata"]["namespace"] == "caritas"


def rejects(overlays, expected, *, release="caritas", args=()):
    try:
        render(*overlays, release=release, helm_args=(*ENABLED, *args))
    except AssertionError as error:
        assert expected in str(error), str(error)
    else:
        raise AssertionError("Pre-Dev access rendered for an invalid release or values")


def main():
    for overlays in ((), ("values-dev.yaml",), ("values-prod.yaml",), PREDEV):
        assert not ACCESS_KEYS.intersection(by_key(render(*overlays)))

    baseline = by_key(render(*PREDEV))
    selected = by_key(render(*PREDEV, helm_args=ENABLED))
    assert set(selected) - set(baseline) == ACCESS_KEYS, set(selected) - set(baseline)
    # Offline renders independently generate these three monitoring passwords.
    for docs in (baseline, selected):
        passwords = docs[("Secret", "oriso-db-monitoring")]["data"]
        for key in ("mariadb-password", "mongodb-password", "rabbitmq-password"):
            assert passwords.pop(key)
    assert all(selected[key] == value for key, value in baseline.items()), "Existing chart objects changed"
    assert_identity(selected, PRIVATE, COMMON_DEPLOYMENTS, [rule("", "services/proxy", "create", ["http:caritas-signoz:8080"])])
    assert_identity(selected, HOSTED, COMMON_DEPLOYMENTS + MATRIXRTC_DEPLOYMENTS, [])
    service = selected[("Service", "caritas-signoz")]
    assert any(port["port"] == 8080 for port in service["spec"]["ports"])

    boundary = "requires the canonical caritas Pre-Dev release"
    rejects(("values-dev.yaml",), boundary)
    rejects(("values-prod.yaml",), boundary)
    rejects(PREDEV, boundary, release="wrong-release")
    rejects(PREDEV, boundary, args=("--namespace", "other"))
    rejects(PREDEV, boundary, args=("--set", "global.domainName=dev.oriso.org"))
    rejects(("values-pre-dev.yaml",), "requires signoz.enabled=true")
    rejects(PREDEV, "must be a boolean", args=("--set-string", f"{FLAG}=false"))
    print("PASS scoped Pre-Dev Kubernetes identities")


if __name__ == "__main__":
    main()
