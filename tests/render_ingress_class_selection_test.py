#!/usr/bin/env python3
"""Keep application Ingresses on the explicit networking.k8s.io/v1 class."""

import re
from pathlib import Path

from render_signoz_dev_observability_test import by_key, render

ROOT = Path(__file__).resolve().parents[1]
LEGACY = "kubernetes.io/ingress.class"
NGINX_CLASS_KEY = ("IngressClass", "nginx")
LITERAL_KEY = re.compile(r'^\s*["\']?kubernetes\.io/ingress\.class["\']?\s*:', re.M)
COMMON = ("main-ingress", "matrix-client-ingress", "acme-challenge-ingress")
# The Permissions-Policy render contract covers its three migrated routes.
HEADER_POLICY_ROUTES = {
    "account-inactivity-api-ingress", "element-call-ingress", "main-ingress",
}
OPTIONAL = (
    "media-scanner-ingress", "media-scanner-direct-media-block-ingress",
    "account-inactivity-api-ingress", "keycloak-token-rate-limit-ingress",
    "storybook-dev-ingress",
)
FEATURES = (
    "--set", "mediaScanner.enabled=true",
    "--set", "mediaScanner.requestSecret.existingSecret=media-scanner-request-secret",
    "--set", "global.accountInactivity.accessGateEnabled=true",
    "--set", "global.keycloak.tokenRateLimit.enabled=true",
    "--set-string", "global.keycloak.serviceTechUserId=00000000-0000-4000-8000-000000000000",
)


def assert_modern_routes(resources, required):
    ingresses = {name: doc for (kind, name), doc in resources.items() if kind == "Ingress"}
    assert ingresses
    for name, doc in ingresses.items():
        if name not in HEADER_POLICY_ROUTES:
            assert LEGACY not in doc.get("metadata", {}).get("annotations", {}), name
    for name in required:
        assert ingresses[name]["spec"]["ingressClassName"] == "nginx", name


def main():
    offenders = [
        str(path.relative_to(ROOT)) for path in (ROOT / "templates").rglob("*.yaml")
        if path.stem not in HEADER_POLICY_ROUTES and LITERAL_KEY.search(path.read_text())
    ]
    assert not offenders, f"deprecated IngressClass annotations in {', '.join(sorted(offenders))}"

    for overlays in ((), ("values-dev.yaml",), ("values-prod.yaml",)):
        assert_modern_routes(by_key(render(*overlays)), COMMON)
    bundled = by_key(render("values-dev.yaml", helm_args=FEATURES))
    assert_modern_routes(bundled, COMMON + OPTIONAL)
    assert bundled[NGINX_CLASS_KEY]["spec"]["controller"] == "k8s.io/ingress-nginx"

    external = by_key(render("values-dev.yaml", helm_args=(*FEATURES, "--set", "nginxController.enabled=false")))
    assert_modern_routes(external, COMMON + OPTIONAL)
    assert NGINX_CLASS_KEY not in external
    print("PASS: application and optional Ingresses keep their explicit controller class")


if __name__ == "__main__":
    main()
