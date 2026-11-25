#!/usr/bin/env python3
"""Preserve ingress routing and edge policies across the staged snippet migration."""

from render_signoz_dev_observability_test import by_key, render

PREFIX = "nginx.ingress.kubernetes.io/"
MODE = "nginxController.useStandardRoutePolicies"
FEATURES = (
    "--set", "global.accountInactivity.accessGateEnabled=true",
    "--set", "global.keycloak.tokenRateLimit.enabled=true",
    "--set-string", "global.keycloak.serviceTechUserId=00000000-0000-4000-8000-000000000000",
)
HEADER_ROUTES = {
    "main-ingress": "permissions-policy-call-host",
    "account-inactivity-api-ingress": "permissions-policy-call-host",
    "element-call-ingress": "permissions-policy-main-host",
}
LIMIT_ROUTES = (
    "keycloak-token-rate-limit-ingress",
    "matrix-login-rate-limit-ingress",
    "userservice-public-entry-ingress",
)
CONTROLLER = ("ConfigMap", "ingress-nginx-controller")


def check_routes(legacy, active):
    for name, headers_name in HEADER_ROUTES.items():
        previous, resulting = legacy[("Ingress", name)], active[("Ingress", name)]
        old = previous["metadata"]["annotations"].copy()
        new = resulting["metadata"]["annotations"].copy()
        assert "kubernetes.io/ingress.class" not in old and "kubernetes.io/ingress.class" not in new
        snippet = old.pop(PREFIX + "configuration-snippet")
        assert ("ConfigMap", headers_name) in active, f"{name}: missing standard response header ConfigMap"
        config_map = active[("ConfigMap", headers_name)]
        expected = f'{config_map["metadata"]["namespace"]}/{headers_name}'
        assert new.pop(PREFIX + "custom-headers", None) == expected, f"{name}: missing standard response header reference"
        assert config_map["data"] == {"Permissions-Policy": snippet.split("'", 2)[1]}
        assert new == old and previous["spec"] == resulting["spec"], name

    for name in LIMIT_ROUTES:
        previous, resulting = legacy[("Ingress", name)], active[("Ingress", name)]
        old = previous["metadata"]["annotations"].copy()
        assert old.pop(PREFIX + "configuration-snippet").strip() == "limit_req_status 429;"
        assert resulting["metadata"]["annotations"] == old and previous["spec"] == resulting["spec"], name


def main():
    legacy = by_key(render(helm_args=FEATURES))
    active = by_key(render(helm_args=(*FEATURES, "--set", f"{MODE}=true")))
    check_routes(legacy, active)
    settings = dict(active[CONTROLLER]["data"])
    assert settings.pop("limit-req-status-code") == "429"
    assert settings.pop("global-allowed-response-headers") == "Permissions-Policy"
    assert settings == legacy[CONTROLLER]["data"]
    header_keys = {("ConfigMap", name) for name in HEADER_ROUTES.values()}
    assert set(legacy) | header_keys == set(active)
    route_keys = {("Ingress", name) for name in (*HEADER_ROUTES, *LIMIT_ROUTES)}
    assert {key for key in legacy if legacy[key] != active[key]} == route_keys | {CONTROLLER}

    remote = ("--set", "nginxController.enabled=false")
    external_legacy = by_key(render(helm_args=(*FEATURES, *remote)))
    external_active = by_key(render(helm_args=(*FEATURES, *remote, "--set", f"{MODE}=true")))
    check_routes(external_legacy, external_active)
    assert CONTROLLER not in external_active
    assert set(external_legacy) | header_keys == set(external_active)
    assert {key for key in external_legacy if external_legacy[key] != external_active[key]} == route_keys

    try:
        render(helm_args=("--set-string", f"{MODE}=false"))
    except AssertionError as error:
        assert f"{MODE} must be a boolean" in str(error)
    else:
        raise AssertionError("String values must not silently switch production routes")

    print("PASS: preserve headers, throttle settings and route targets in both controller modes")


if __name__ == "__main__":
    main()
