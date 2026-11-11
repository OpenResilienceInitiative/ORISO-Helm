#!/usr/bin/env python3
"""Render the SigNoz exposure and export defaults with synthetic values."""

from pathlib import Path
from tempfile import NamedTemporaryFile
import unittest

import yaml

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
E2E_INGRESS = f"{UI}-e2e-ingress"
EXPORT_FLAGS = (
    "MANAGEMENT_TRACING_EXPORT_OTLP_ENABLED",
    "MANAGEMENT_OTLP_METRICS_EXPORT_ENABLED",
)


def render_process(overlay=PROD_OVERLAY, *settings, inherited_values=None):
    domain = OVERLAYS.get(overlay, {}).get("domain", VALID_TEST_DOMAIN)
    overlays = ("values-dev.yaml", overlay) if overlay == "values-pre-dev.yaml" else (overlay,)
    values = [argument for name in overlays for argument in ("-f", str(Path(CHART_DIR, name)))]
    inherited = ("-f", inherited_values) if inherited_values else ()
    return helm_template(
        *inherited,
        *values,
        "--set-string", f"global.domainName={domain}",
        "--set-string", f"signoz.signoz.env.signoz_global_external__url=https://{domain}/signoz",
        "--set", "signoz.enabled=true",
        *settings,
    )


def render(overlay=PROD_OVERLAY, *settings, inherited_values=None):
    return rendered(render_process(overlay, *settings, inherited_values=inherited_values), overlay)


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


class SigNozExposureDefaults(unittest.TestCase):
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

    def assert_private_preparation(self, docs):
        self.assertEqual(find(docs, "StatefulSet", UI)["spec"]["replicas"], 1)
        self.assertEqual(find(docs, "Deployment", COLLECTOR)["spec"]["replicas"], 0)
        for service in (UI, COLLECTOR):
            spec = find(docs, "Service", service)["spec"]
            self.assertEqual(spec["type"], "ClusterIP")
            self.assertFalse(spec.get("externalIPs"))
            self.assertTrue(all(port.get("nodePort") in (None, 0) for port in spec["ports"]))
        ports = {port["port"] for port in find(docs, "Service", COLLECTOR)["spec"]["ports"]}
        self.assertFalse(ports & {4319, 4321})
        self.assert_export_flags(docs, "false")
        java = configmap_data(docs, "userservice-configmap-env")
        self.assertTrue(java["MANAGEMENT_OTLP_METRICS_EXPORT_URL"].startswith(f"http://{COLLECTOR}."))
        self.assertEqual(configmap_data(docs, "frontend-configmap")["REACT_APP_OTEL_METRICS_URL"], "")
        self.assertEqual(configmap_data(docs, "admin-configmap")["VITE_OTEL_METRICS_URL"], "")
        self.assertEqual(list(signoz_ingresses(docs)), [])
        self.assertFalse(any(
            doc["kind"] in ("HorizontalPodAutoscaler", "ScaledObject")
            and doc["metadata"]["name"] == COLLECTOR
            for doc in docs
        ))
        self.assertFalse(any(
            doc["metadata"]["name"] in (
                f"{UI}-otel-agent", f"{UI}-otel-cluster", "db-monitoring-reconcile", f"{UI}-observability",
            )
            or "signoz-otel-gateway" in doc["metadata"]["name"]
            for doc in docs
        ))

    def test_production_backend_keeps_the_ui_without_collecting(self):
        self.assert_private_preparation(render())

    def test_production_overrides_inherited_receivers_and_public_routes(self):
        lower = yaml.safe_load(Path(CHART_DIR, "values-dev.yaml").read_text())
        lower["global"]["observability"].update({
            "otlpEnabled": True,
            "otlpCollectorHost": "inherited-dev.render.oriso.internal:4318",
            "e2eTraceIngest": {"enabled": True, "authSecret": "render-e2e-auth"},
        })
        signoz = lower["signoz"]
        signoz["orisoIngress"] = {"enabled": True}
        signoz["orisoObservability"] = {"enabled": True}
        signoz["signoz-otel-gateway"] = {"enabled": True}
        signoz["signoz"] = {
            "ingress": {"enabled": True},
            "service": {"type": "NodePort", "nodePort": 30080},
        }
        signoz["otelCollector"].update({
            "replicaCount": 2,
            "autoscaling": {"enabled": True, "keda": {"enabled": True}},
            "ingress": {"enabled": True},
            "service": {"type": "LoadBalancer"},
            "ports": {"otlp-rum": {"enabled": True}, "otlp-e2e": {"enabled": True}},
        })
        with NamedTemporaryFile(mode="w", suffix=".yaml", encoding="utf-8") as inherited:
            yaml.safe_dump(lower, inherited)
            inherited.flush()
            self.assert_private_preparation(render(inherited_values=inherited.name))

    def test_local_java_export_fails_when_the_bundled_collector_is_dormant(self):
        for host in ("", f"{COLLECTOR}:4318", f"{COLLECTOR}.default.svc.cluster.local.:4318"):
            with self.subTest(host=host):
                extra = ("--set-string", f"global.observability.otlpCollectorHost={host}") if host else ()
                process = render_process(PROD_OVERLAY, "--set", "global.observability.otlpEnabled=true", *extra)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn("bundled SigNoz collector has zero replicas", process.stderr)

    def test_local_rum_and_infrastructure_routes_require_an_active_collector(self):
        for setting in (
            "global.observability.webVitals.enabled",
            "signozCollector.enabled",
            "signoz.otelCollector.ingress.enabled",
        ):
            with self.subTest(setting=setting):
                process = render_process(PROD_OVERLAY, "--set", f"{setting}=true")
                self.assertNotEqual(process.returncode, 0)
                self.assertIn("bundled SigNoz collector has zero replicas", process.stderr)

    def test_explicit_local_export_and_receiver_keep_ingress_off(self):
        docs = render(
            PROD_OVERLAY, "--set", "global.observability.otlpEnabled=true",
            "--set", "signoz.otelCollector.replicaCount=1",
        )
        self.assert_export_flags(docs, "true")
        self.assertEqual(find(docs, "Deployment", COLLECTOR)["spec"]["replicas"], 1)
        self.assertEqual(list(signoz_ingresses(docs)), [])

    def test_explicit_external_export_keeps_the_bundled_collector_dormant(self):
        host = "external-otlp.render.oriso.internal:4318"
        docs = render(
            PROD_OVERLAY, "--set", "global.observability.otlpEnabled=true",
            "--set-string", f"global.observability.otlpCollectorHost={host}",
        )
        self.assert_export_flags(docs, "true")
        self.assertEqual(find(docs, "Deployment", COLLECTOR)["spec"]["replicas"], 0)
        data = configmap_data(docs, "userservice-configmap-env")
        self.assertEqual(data["MANAGEMENT_OTLP_METRICS_EXPORT_URL"], f"http://{host}/v1/metrics")
        self.assertEqual(list(signoz_ingresses(docs)), [])

    def test_explicit_ui_opt_in_keeps_export_and_collector_off(self):
        docs = render(PROD_OVERLAY, "--set", "signoz.orisoIngress.enabled=true")
        self.assert_export_flags(docs, "false")
        self.assertEqual(find(docs, "Deployment", COLLECTOR)["spec"]["replicas"], 0)
        self.assertEqual(list(signoz_ingresses(docs)), [UI_INGRESS])

    def test_dev_retains_default_export_and_ui(self):
        docs = render("values-dev.yaml")
        self.assert_export_flags(docs, "true")
        self.assertEqual(find(docs, "Deployment", COLLECTOR)["spec"]["replicas"], 1)
        # The dedicated ingest and the TLS alias have separate Dev contracts.
        optional = {E2E_INGRESS, "signoz-oriso-org-redirect"}
        self.assertEqual(set(signoz_ingresses(docs)) - optional, {UI_INGRESS, RUM_INGRESS})

    def test_predev_keeps_ui_private_even_with_inherited_public_settings(self):
        with NamedTemporaryFile(mode="w", suffix=".yaml", encoding="utf-8") as inherited:
            inherited.write("""signoz:
  orisoIngress:
    enabled: true
  signoz:
    service:
      type: NodePort
    ingress:
      enabled: true
      hosts:
        - host: alternate.render.oriso.internal
          paths:
            - path: /
              pathType: Prefix
              port: 8080
""")
            inherited.flush()
            for values in (None, inherited.name):
                with self.subTest(inherited_public_settings=values is not None):
                    docs = render("values-pre-dev.yaml", inherited_values=values)
                    self.assert_export_flags(docs, "true")
                    ui_service = find(docs, "Service", UI)["spec"]
                    self.assertEqual(ui_service["type"], "ClusterIP")
                    self.assertTrue(any(port["port"] == 8080 for port in ui_service["ports"]))
                    self.assertTrue(all(port.get("nodePort") in (None, 0) for port in ui_service["ports"]))
                    self.assertEqual(list(signoz_ingresses(docs)), [RUM_INGRESS])
                    rum = find(docs, "Ingress", RUM_INGRESS)["spec"]["rules"][0]["http"]["paths"][0]
                    self.assertEqual(rum["path"], "/signoz-rum/v1/metrics")
                    self.assertEqual(rum["pathType"], "Exact")
                    self.assertEqual(rum["backend"]["service"], {"name": COLLECTOR, "port": {"number": 4319}})

    def test_explicit_predev_ui_opt_in_preserves_rum(self):
        docs = render("values-pre-dev.yaml", "--set", "signoz.orisoIngress.enabled=true")
        self.assert_export_flags(docs, "true")
        self.assertCountEqual(signoz_ingresses(docs), [UI_INGRESS, RUM_INGRESS])

    def test_explicitly_disabling_ui_preserves_the_separate_rum_ingress(self):
        docs = render("values-dev.yaml", "--set", "signoz.orisoIngress.enabled=false")
        self.assert_export_flags(docs, "true")
        self.assertEqual(set(signoz_ingresses(docs)) - {E2E_INGRESS}, {RUM_INGRESS})


if __name__ == "__main__":
    unittest.main()
