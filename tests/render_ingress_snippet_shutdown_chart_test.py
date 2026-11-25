#!/usr/bin/env python3
"""Render the real chart with every snippet migration on and snippet annotations disabled."""

import re

from render_signoz_dev_observability_test import by_key, render

SNIPPET = re.compile(r"^nginx\.ingress\.kubernetes\.io/[a-z-]+-snippet$")
SHUTDOWN = ("--set", "nginxController.disableSnippetAnnotations=true")
MIGRATIONS = (
    "nginxController.useStandardRoutePolicies",
    "matrix.routeServerDiscoveryViaSynapse",
    "signoz.orisoIngress.useRouteGateway",
)
# Turn on every optional route that rendered a snippet before the migration.
FEATURES = (
    "--set", "nginxController.enabled=true",
    "--set", "nginxController.scopeToReleaseNamespace=true",
    "--set", "mediaScanner.enabled=true",
    "--set", "mediaScanner.blockDirectMediaAccess=true",
    "--set-string", "mediaScanner.requestSecret.existingSecret=media-scanner-request",
    "--set", "global.observability.webVitals.enabled=true",
    "--set", "signoz.otelCollector.ports.otlp-rum.enabled=true",
    "--set", "healthDashboard.enabled=true",
    # Prepares the gateway only; useRouteGateway is the switch that moves the routes.
    "--set", "signoz.orisoIngress.prepareRouteGateway=true",
)


def settings(skip=None):
    args = list(FEATURES)
    for key in MIGRATIONS:
        args += ["--set", f"{key}={str(key != skip).lower()}"]
    return tuple(args)


def main():
    docs = by_key(render("values-dev.yaml", helm_args=(*settings(), *SHUTDOWN)))
    ingresses = [doc for (kind, _), doc in docs.items() if kind == "Ingress"]
    assert ingresses, "the activated profile renders no Ingress"
    snippets = sorted(
        doc["metadata"]["name"]
        for doc in ingresses
        for key in (doc["metadata"].get("annotations") or {})
        if SNIPPET.match(key)
    )
    assert not snippets, f"snippet annotations left after the shutdown: {snippets}"
    controller = docs[("ConfigMap", "ingress-nginx-controller")]["data"]
    assert controller["allow-snippet-annotations"] == "false"
    assert controller["annotations-risk-level"] == "High"

    # A migration left off must still stop the shutdown on the real chart.
    for key in MIGRATIONS:
        try:
            render("values-dev.yaml", helm_args=(*settings(skip=key), *SHUTDOWN))
        except AssertionError as error:
            # Only the shutdown guard counts; another validation failing would hide a gap.
            assert "cannot disable ingress snippets" in str(error), f"{key}: {error}"
            continue
        raise AssertionError(f"shutdown rendered although {key} was off")

    print(f"PASS: {len(ingresses)} Ingresses render without snippets; each skipped migration blocks the shutdown")


if __name__ == "__main__":
    main()
