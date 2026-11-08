"""Render contracts for the opt-in SigNoz private OTLP transport."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

import yaml

from render_signoz_dev_observability_test import by_key, render


NAME = "caritas-signoz-otel-collector"
MODE_LABEL = "oriso.org/private-otlp"
PROXY_PORT = 4322
BODY_LIMIT = 262144
SENDER_LABELS = {"app": "private-otlp-rehearsal"}

POLICY = {
    "filter": {
        "traces": {"span": [
            'resource.attributes["service.name"] != "oriso-signoz-acceptance"',
            'span.attributes["probe.error"] != nil and ParseJSON(span.attributes["probe.error"]) != nil',
        ]},
        "metrics": {"metric": ['metric.name != "oriso.signoz.acceptance"']},
    },
    "transform": {
        "trace_statements": [
            'keep_keys(resource.attributes, ["service.name"])',
            'set(span.name, "synthetic private OTLP")',
            'keep_keys(span.attributes, [])',
        ],
        "metric_statements": [
            'keep_keys(resource.attributes, ["service.name"])',
            'keep_keys(datapoint.attributes, [])',
        ],
    },
}


def values():
    return {
        "global": {"domainName": "prod.render.oriso.internal", "observability": {"deploymentEnvironment": "production"}},
        "signoz": {
            "enabled": True,
            "orisoIngress": {"enabled": False},
            "otelCollector": {"privateOtlp": {
                "enabled": True,
                "producerMatchLabels": [SENDER_LABELS],
                "maxRequestBytes": BODY_LIMIT,
                "policy": deepcopy(POLICY),
            }},
        },
    }


def render_private(overlay=None):
    with tempfile.TemporaryDirectory(prefix="private-otlp-render-") as directory:
        file = Path(directory) / "overlay.yaml"
        file.write_text(yaml.safe_dump(values() if overlay is None else overlay), encoding="utf-8")
        return by_key(render("values-prod.yaml", str(file)))


class PrivateOtlpRenderTest(unittest.TestCase):
    def test_private_wire_boundary_and_reviewed_policy(self):
        docs = render_private()
        manifest = docs[("ConfigMap", NAME)]
        config = yaml.safe_load(manifest["data"]["otel-collector-config.yaml"])
        self.assertEqual(set(config["receivers"]), {"otlp"})
        self.assertEqual(config["receivers"]["otlp"]["protocols"], {"http": {
            "endpoint": "127.0.0.1:4318", "max_request_body_size": BODY_LIMIT,
            "compression_algorithms": ["gzip"],
        }})
        self.assertEqual(set(config["extensions"]), {"health_check"})
        self.assertNotIn("otel-collector-opamp-config.yaml", manifest["data"])
        self.assertIn("private-otlp.conf", manifest["data"])
        self.assertIn("private-otlp-error.pb", manifest["binaryData"])
        pipelines = config["service"]["pipelines"]
        self.assertEqual(set(pipelines), {"traces", "metrics", "metrics/meter"})
        for signal in ("traces", "metrics"):
            self.assertEqual(pipelines[signal]["receivers"], ["otlp"])
            self.assertEqual(pipelines[signal]["processors"][:3],
                             ["memory_limiter", "filter/oriso-private", "transform/oriso-private"])
        for processor in ("filter/oriso-private", "transform/oriso-private"):
            self.assertEqual(config["processors"][processor]["error_mode"], "propagate")

        workload = docs[("Deployment", NAME)]["spec"]
        self.assertNotIn(MODE_LABEL, workload["selector"]["matchLabels"])
        self.assertEqual(workload["template"]["metadata"]["labels"][MODE_LABEL], "true")
        pod = workload["template"]["spec"]
        collector, proxy = pod["containers"]
        self.assertNotIn("ports", collector)
        self.assertFalse(any("manager-config" in arg for arg in collector["args"]))
        self.assertRegex(collector["image"], r"signoz-otel-collector@sha256:[a-f0-9]{64}$")
        self.assertRegex(proxy["image"], r"nginx-unprivileged@sha256:[a-f0-9]{64}$")
        self.assertEqual(proxy["ports"], [{"name": "otlp-guard", "containerPort": PROXY_PORT, "protocol": "TCP"}])
        self.assertTrue(proxy["securityContext"]["readOnlyRootFilesystem"])
        self.assertTrue(any(v.get("emptyDir", {}).get("medium") == "Memory" for v in pod["volumes"]))

        service = docs[("Service", NAME)]["spec"]
        self.assertEqual(service["type"], "ClusterIP")
        self.assertEqual(service["selector"][MODE_LABEL], "true")
        self.assertEqual(service["ports"],
                         [{"name": "otlp-http", "port": 4318, "targetPort": "otlp-guard", "protocol": "TCP"}])
        policy = docs[("NetworkPolicy", NAME + "-private-otlp")]["spec"]
        self.assertEqual(policy["podSelector"], workload["selector"])
        self.assertEqual(policy["ingress"], [{
            "from": [{"podSelector": {"matchLabels": SENDER_LABELS}}],
            "ports": [{"protocol": "TCP", "port": PROXY_PORT}],
        }])

    def test_default_preserves_the_existing_collector(self):
        overlay = values()
        del overlay["signoz"]["otelCollector"]["privateOtlp"]
        docs = render_private(overlay)
        manifest = docs[("ConfigMap", NAME)]
        self.assertIn("otel-collector-opamp-config.yaml", manifest["data"])
        self.assertNotIn("private-otlp.conf", manifest["data"])
        service = docs[("Service", NAME)]["spec"]
        self.assertNotIn(MODE_LABEL, service["selector"])
        self.assertIn(4317, {port["port"] for port in service["ports"]})
        self.assertNotIn(("NetworkPolicy", NAME + "-private-otlp"), docs)

    def test_rejects_incomplete_or_bypassable_modes(self):
        cases = (
            (("global", "observability", "deploymentEnvironment"), "dev", "production"),
            (("signoz", "otelCollector", "privateOtlp", "producerMatchLabels"), [{}], "producerMatchLabels"),
            (("signoz", "otelCollector", "privateOtlp", "maxRequestBytes"), 0, "maxRequestBytes"),
            (("signoz", "otelCollector", "privateOtlp", "policy", "filter", "error_mode"), "ignore", "propagate"),
            (("signoz", "otelCollector", "privateOtlp", "policy", "transform", "metric_statements"), [], "metric_statements"),
            (("signoz", "otelCollector", "service", "type"), "NodePort", "ClusterIP"),
            (("signoz", "otelCollector", "configMap", "create"), False, "configMap.create"),
            (("signoz", "otelCollector", "command", "extraArgs"), ["--config=/unreviewed"], "custom"),
            (("signoz", "otelCollector", "ingress", "enabled"), True, "ingress"),
            (("global", "observability", "webVitals", "enabled"), True, "RUM"),
            (("global", "observability", "e2eTraceIngest", "enabled"), True, "E2E"),
            (("signozCollector", "enabled"), True, "infrastructure"),
        )
        for path, value, message in cases:
            with self.subTest(path=path):
                overlay = values()
                branch = overlay
                for key in path[:-1]:
                    branch = branch.setdefault(key, {})
                branch[path[-1]] = value
                with self.assertRaisesRegex(AssertionError, message):
                    render_private(overlay)


if __name__ == "__main__":
    unittest.main(verbosity=2)
