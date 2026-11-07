#!/usr/bin/env python3
"""Render the production SigNoz opt-ins with synthetic values."""

from pathlib import Path
import unittest

from render_required_public_urls_test import (
    CHART_DIR,
    OVERLAYS,
    PROD_OVERLAY,
    VALID_TEST_DOMAIN,
    configmap_data,
    helm_template,
    rendered,
)
from render_signoz_k8s_infra_test import env_by_name, find

RELEASE = "required-public-urls"
UI = f"{RELEASE}-signoz"
COLLECTOR = f"{UI}-otel-collector"
UI_INGRESS = f"{UI}-ingress"
RUM_INGRESS = f"{UI}-rum-ingress"
EXPORT_FLAGS = (
    "MANAGEMENT_TRACING_EXPORT_OTLP_ENABLED",
    "MANAGEMENT_OTLP_METRICS_EXPORT_ENABLED",
)


def render(overlay=PROD_OVERLAY, *settings):
    domain = OVERLAYS.get(overlay, {}).get("domain", VALID_TEST_DOMAIN)
    overlays = ("values-dev.yaml", overlay) if overlay == "values-pre-dev.yaml" else (overlay,)
    values = [argument for name in overlays for argument in ("-f", str(Path(CHART_DIR, name)))]
    proc = helm_template(
        *values,
        "--set-string", f"global.domainName={domain}",
        "--set-string", f"signoz.signoz.env.signoz_global_external__url=https://{domain}/signoz",
        "--set", "signoz.enabled=true",
        *settings,
    )
    return rendered(proc, overlay)


def signoz_ingresses(docs):
    for item in docs:
        if item["kind"] != "Ingress":
            continue
        spec = item["spec"]
        backends = [spec.get("defaultBackend", {})]
        for rule in spec.get("rules", []):
            backends.extend(path["backend"] for path in rule.get("http", {}).get("paths", []))
        if any(backend.get("service", {}).get("name") in (UI, COLLECTOR) for backend in backends):
            yield item["metadata"]["name"]


class ProductionSigNozDefaults(unittest.TestCase):
    def assert_export_flags(self, docs, expected):
        for name in ("agencyservice", "consultingtypeservice", "tenantservice", "userservice"):
            with self.subTest(service=name):
                configmap = f"{name}-configmap-env"
                env = env_by_name(find(docs, "Deployment", name))
                data = configmap_data(docs, configmap)
                for flag in EXPORT_FLAGS:
                    self.assertEqual(env[flag]["valueFrom"]["configMapKeyRef"], {
                        "name": configmap,
                        "key": flag,
                    })
                    self.assertEqual(data[flag], expected, flag)

    def test_production_backend_does_not_auto_enable_export_or_ingress(self):
        docs = render()
        for service in (UI, COLLECTOR):
            self.assertEqual(find(docs, "Service", service)["spec"].get("type", "ClusterIP"), "ClusterIP")
        self.assert_export_flags(docs, "false")
        self.assertEqual(list(signoz_ingresses(docs)), [])

    def test_explicit_export_opt_in_keeps_ingress_off(self):
        docs = render(PROD_OVERLAY, "--set", "global.observability.otlpEnabled=true")
        self.assert_export_flags(docs, "true")
        self.assertEqual(list(signoz_ingresses(docs)), [])

    def test_explicit_ui_opt_in_keeps_export_off(self):
        docs = render(PROD_OVERLAY, "--set", "signoz.orisoIngress.enabled=true")
        self.assert_export_flags(docs, "false")
        self.assertEqual(list(signoz_ingresses(docs)), [UI_INGRESS])

    def test_nonproduction_retains_default_export_and_ui(self):
        for overlay in OVERLAYS:
            with self.subTest(overlay=overlay):
                docs = render(overlay)
                self.assert_export_flags(docs, "true")
                self.assertCountEqual(signoz_ingresses(docs), [UI_INGRESS, RUM_INGRESS])

    def test_explicitly_disabling_ui_preserves_the_separate_rum_ingress(self):
        docs = render("values-pre-dev.yaml", "--set", "signoz.orisoIngress.enabled=false")
        self.assert_export_flags(docs, "true")
        self.assertEqual(list(signoz_ingresses(docs)), [RUM_INGRESS])


if __name__ == "__main__":
    unittest.main()
