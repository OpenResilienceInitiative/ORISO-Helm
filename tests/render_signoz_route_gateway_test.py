#!/usr/bin/env python3
"""Preserve the public SigNoz contracts during the snippet-free cutover."""

from render_signoz_dev_observability_test import by_key, render

GATEWAY = "caritas-signoz-route-gateway"
PREPARE = "signoz.orisoIngress.prepareRouteGateway=true"
USE = "signoz.orisoIngress.useRouteGateway=true"
PREFIX = "nginx.ingress.kubernetes.io/"
ROUTES = ("caritas-signoz-rum-ingress", "caritas-signoz-e2e-ingress")


def dev(*settings):
    args = tuple(value for setting in settings for value in ("--set", setting))
    return by_key(render("values-dev.yaml", helm_args=args))


def path(ingress):
    return ingress["spec"]["rules"][0]["http"]["paths"][0]


def main():
    old, prepared, switched = dev(), dev(PREPARE), dev(PREPARE, USE)
    ui = ("Ingress", "caritas-signoz-ingress")
    redirect = ("Ingress", "caritas-signoz-slash-redirect")
    gateway_keys = {(kind, GATEWAY) for kind in ("Deployment", "ConfigMap", "Service", "PodDisruptionBudget")}
    assert gateway_keys.isdisjoint(old)
    assert gateway_keys <= prepared.keys()
    assert redirect not in prepared
    assert all(old[key] == prepared[key] for key in (ui, *(("Ingress", name) for name in ROUTES)))
    assert all(prepared[key] == switched[key] for key in gateway_keys)
    assert path(switched[ui]) == path(old[ui])
    assert path(switched[redirect])["path"] == "/(?-i:signoz)$"
    assert switched[redirect]["spec"]["tls"] == old[ui]["spec"]["tls"]
    expected_backend = {"service": {"name": GATEWAY, "port": {"name": "http"}}}
    assert path(switched[redirect])["backend"] == expected_backend
    for name in ROUTES:
        key = ("Ingress", name)
        assert path(switched[key]) == {**path(old[key]), "backend": expected_backend}
        assert switched[key]["metadata"]["annotations"] == {
            name: value for name, value in old[key]["metadata"]["annotations"].items()
            if name != PREFIX + "configuration-snippet"
        }
    for key in (ui, redirect):
        assert PREFIX + "configuration-snippet" not in switched[key]["metadata"]["annotations"]

    conf = switched[("ConfigMap", GATEWAY)]["data"]["nginx.conf"]
    assert "return 301 https://dev.oriso.org/signoz/$is_args$args;" in conf
    assert "proxy_pass http://caritas-signoz-otel-collector:4319/v1/metrics;" in conf
    assert "proxy_pass http://caritas-signoz-otel-collector:4321/v1/traces;" in conf
    assert 'if ($request_uri != "/signoz-e2e/v1/traces") { return 404; }' in conf
    pod = switched[("Deployment", GATEWAY)]["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"] is True
    for name in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"):
        assert f"{name}_temp_path /tmp/" in conf
    assert all("secret" not in volume for volume in pod["volumes"])

    rum_only = dev(PREPARE, USE, "signoz.orisoIngress.enabled=false",
                   "global.observability.e2eTraceIngest.enabled=false",
                   "signoz.otelCollector.ports.otlp-e2e.enabled=false",
                   "signoz.otelCollector.ports.otlp-rum.servicePort=4400")
    assert ui not in rum_only and redirect not in rum_only
    isolated = rum_only[("ConfigMap", GATEWAY)]["data"]["nginx.conf"]
    assert "http://caritas-signoz-otel-collector:4400/v1/metrics" in isolated
    assert "/v1/traces" not in isolated and "/signoz/" not in isolated
    try:
        dev(USE)
    except AssertionError as error:
        assert "useRouteGateway=true requires" in str(error), error
    else:
        raise AssertionError("the gateway switch was allowed before preparation")
    print("PASS: staged SigNoz routes preserve auth and forward only their configured signal")


if __name__ == "__main__":
    main()
