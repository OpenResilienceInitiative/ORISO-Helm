"""Render RabbitMQ ingress against the broker and its chart-generated peers."""

import unittest

from render_addonservice_test import RENDER_ONLY_KEY
from render_signoz_dev_observability_test import by_key, render


POLICY = ("NetworkPolicy", "rabbitmq-ingress")
ENABLED = ("--set", "networkPolicies.rabbitmqIngress.enabled=true")
NAMESPACE = "caritas"
RELEASE = "rabbitmq-policy-test"
COLLECTOR = ("Deployment", f"{RELEASE}-k8s-infra-otel-deployment")
MONITORING_JOB = ("Job", "db-monitoring-reconcile")


def resources(*overlays, helm_args=()):
    return by_key(render(*overlays, release=RELEASE, helm_args=helm_args))


class RabbitMqIngressPolicyTest(unittest.TestCase):
    def assert_policy(self, chart, *, addon=False, db_metrics=False):
        self.assertTrue(POLICY in chart, "Enabled RabbitMQ ingress policy missing")
        policy = chart[POLICY]
        self.assertEqual(policy["metadata"]["namespace"], NAMESPACE)

        def peer(kind, name, labels):
            workload = chart[kind, name]
            self.assertEqual(workload["metadata"].get("namespace", NAMESPACE), NAMESPACE)
            pod_labels = workload["spec"]["template"]["metadata"]["labels"]
            self.assertTrue(labels.items() <= pod_labels.items(), f"{kind}/{name}")
            return {"podSelector": {"matchLabels": labels}}

        broker = chart["Deployment", "rabbitmq"]
        target = broker["spec"]["template"]["metadata"]["labels"]
        service = chart["Service", "rabbitmq"]["spec"]
        self.assertEqual(target["app"], "rabbitmq")
        self.assertEqual(service["selector"], {"app": target["app"]})
        ports = {item["name"]: item["targetPort"] for item in service["ports"]}
        self.assertEqual(ports, {"amqp": 5672, "management": 15672})

        app_names = ["userservice", "tenantservice"]
        if addon:
            app_names.append("addonservice")
        else:
            self.assertNotIn(("Deployment", "addonservice"), chart.keys())
        rules = [{
            "from": [peer("Deployment", name, {"app": name}) for name in app_names],
            "ports": [{"protocol": "TCP", "port": ports["amqp"]}],
        }]
        if db_metrics:
            rules.append({
                "from": [
                    peer(*MONITORING_JOB, {"app": "db-monitoring-reconcile"}),
                    peer(*COLLECTOR, {
                        "app.kubernetes.io/name": "k8s-infra",
                        "app.kubernetes.io/instance": RELEASE,
                        "app.kubernetes.io/component": "otel-deployment",
                    }),
                ],
                "ports": [{"protocol": "TCP", "port": ports["management"]}],
            })
        else:
            self.assertNotIn(MONITORING_JOB, chart.keys())
        self.assertEqual(policy["spec"], {
            "podSelector": {"matchLabels": {"app": target["app"]}},
            "policyTypes": ["Ingress"],
            "ingress": rules,
        })

    def test_policy_requires_explicit_opt_in(self):
        for overlays in ((), ("values-dev.yaml",)):
            with self.subTest(overlays=overlays):
                self.assertNotIn(POLICY, resources(*overlays).keys())

    def test_opt_in_admits_only_application_peers_without_observability(self):
        base = resources()
        enabled = resources(helm_args=ENABLED)
        self.assert_policy(enabled)
        for kind, name in (("Deployment", "rabbitmq"), ("Service", "rabbitmq")):
            self.assertEqual(enabled[kind, name], base[kind, name])
        other_policies = {key for key in enabled if key[0] == "NetworkPolicy" and key != POLICY}
        self.assertEqual(other_policies, {key for key in base if key[0] == "NetworkPolicy"})
        for key in other_policies:
            self.assertEqual(enabled[key], base[key])

    def test_addon_and_dev_monitoring_peers_use_different_ports(self):
        dev = resources("values-dev.yaml", helm_args=(
            *ENABLED, "--set", "addonService.enabled=true",
            "--set-string", "global.secrets.addonServiceDbPassword=render-only-db-password",
            "--set-string", f"addonService.tokenPrivateKeyPem={RENDER_ONLY_KEY}",
        ))
        self.assert_policy(dev, addon=True, db_metrics=True)

    def test_collector_without_database_metrics_has_no_management_rule(self):
        dev = resources("values-dev.yaml", helm_args=(
            *ENABLED, "--set", "global.observability.dbMetrics=false",
        ))
        self.assertTrue(COLLECTOR in dev, "Dev collector fixture is missing")
        self.assert_policy(dev)

    def test_nonboolean_enabled_value_fails(self):
        with self.assertRaisesRegex(
            AssertionError, "networkPolicies.rabbitmqIngress.enabled must be a boolean"
        ):
            resources(helm_args=("--set-string", "networkPolicies.rabbitmqIngress.enabled=false"))


if __name__ == "__main__":
    unittest.main()
