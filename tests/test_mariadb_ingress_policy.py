"""Render the opt-in MariaDB ingress rule against its actual chart clients."""

import unittest

from render_addonservice_test import RENDER_ONLY_KEY
from test_network_policy_scope import MATRIXRTC_POLICIES, NAMESPACE, RELEASE, render


POLICY = ("NetworkPolicy", "mariadb-ingress")
MONGODB_POLICY = ("NetworkPolicy", "mongodb-ingress")
MARIADB_ENABLED = ("--set", "networkPolicies.mariadbIngress.enabled=true")
ADDON_AND_MONGODB_ENABLED = (
    "--set", "addonService.enabled=true,networkPolicies.mongodbIngress.enabled=true",
    "--set-string", "global.secrets.addonServiceDbPassword=render-only-db-password",
    "--set-string", f"addonService.tokenPrivateKeyPem={RENDER_ONLY_KEY}",
)
APP_CONSUMERS = {
    ("Deployment", name): {"app": name}
    for name in ("agencyservice", "consultingtypeservice", "keycloak", "tenantservice", "userservice")
}
BOOTSTRAP_CONSUMERS = {
    ("Job", name): {"app": name} for name in ("tenant-bootstrap", "topic-bootstrap")
}


class MariaDbIngressPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.defaults = render()
        cls.omitted = render("--set-json", "networkPolicies=null")
        cls.base_enabled = render(*MARIADB_ENABLED)
        cls.without_tenant_bootstrap = render(
            *MARIADB_ENABLED, "--set", "tenantService.bootstrapTenants.enabled=false",
        )
        cls.without_topic_bootstrap = render(
            *MARIADB_ENABLED, "--set", "consultingTypeService.bootstrapTopics.enabled=false",
        )
        cls.dev_with_addon = render(*MARIADB_ENABLED, *ADDON_AND_MONGODB_ENABLED, dev=True)
        cls.dev_without_mariadb_policy = render(
            *ADDON_AND_MONGODB_ENABLED,
            "--set", "networkPolicies.mariadbIngress.enabled=false",
            "--set", "networkPolicies.namespaceIngress.enabled=false",
            dev=True,
        )

    def assert_policy(self, resources, consumers):
        policy = resources.get(POLICY)
        self.assertIsNotNone(policy, "MariaDB ingress policy must render when enabled")
        self.assertEqual(policy["metadata"]["namespace"], NAMESPACE)
        spec = policy["spec"]
        self.assertEqual(spec["policyTypes"], ["Ingress"])
        self.assertEqual(spec["podSelector"], {"matchLabels": {"app": "mariadb"}})
        self.assertNotIn("egress", spec)

        datastore = resources["StatefulSet", "mariadb"]["spec"]["template"]
        self.assertEqual(datastore["metadata"]["labels"]["app"], "mariadb")
        [ingress] = spec["ingress"]
        [service_port] = resources["Service", "mariadb"]["spec"]["ports"]
        self.assertEqual(service_port["targetPort"], 3306)
        self.assertEqual(ingress["ports"], [{"protocol": "TCP", "port": service_port["targetPort"]}])

        peers = []
        for (kind, name), labels in consumers.items():
            resource = resources[kind, name]
            self.assertEqual(resource["metadata"].get("namespace", NAMESPACE), NAMESPACE)
            pod_labels = resource["spec"]["template"]["metadata"]["labels"]
            self.assertTrue(labels.items() <= pod_labels.items(), f"{kind}/{name}")
            peers.append({"podSelector": {"matchLabels": labels}})
        self.assertCountEqual(ingress["from"], peers)

    def test_policy_requires_explicit_opt_in(self):
        for resources in (self.defaults, self.omitted, self.dev_without_mariadb_policy):
            self.assertFalse(POLICY in resources, "MariaDB ingress policy should require explicit opt-in")

    def test_opt_in_admits_only_application_and_enabled_bootstrap_pods(self):
        self.assert_policy(self.base_enabled, {**APP_CONSUMERS, **BOOTSTRAP_CONSUMERS})
        for resources, enabled_hook, disabled_hook in (
            (self.without_tenant_bootstrap, "topic-bootstrap", "tenant-bootstrap"),
            (self.without_topic_bootstrap, "tenant-bootstrap", "topic-bootstrap"),
        ):
            with self.subTest(disabled_hook=disabled_hook):
                self.assertFalse(("Job", disabled_hook) in resources)
                self.assert_policy(resources, {**APP_CONSUMERS, ("Job", enabled_hook): {"app": enabled_hook}})

    def test_dev_with_addon_admits_both_addon_peers_and_both_monitoring_peers(self):
        resources = self.dev_with_addon
        [addon_db_job] = [
            key for key in resources if key[0] == "Job" and key[1].startswith("addonservice-db-init-")
        ]
        self.assert_policy(
            resources,
            {
                **APP_CONSUMERS,
                **BOOTSTRAP_CONSUMERS,
                ("Deployment", "addonservice"): {"app": "addonservice"},
                addon_db_job: {"app": "addonservice-db-init"},
                ("Job", "db-monitoring-reconcile"): {"app": "db-monitoring-reconcile"},
                ("Deployment", f"{RELEASE}-k8s-infra-otel-deployment"): {
                    "app.kubernetes.io/name": "k8s-infra",
                    "app.kubernetes.io/instance": RELEASE,
                    "app.kubernetes.io/component": "otel-deployment",
                },
            },
        )

    def test_opt_in_does_not_change_mongodb_or_matrixrtc_policies(self):
        for policy in (MONGODB_POLICY, *MATRIXRTC_POLICIES):
            self.assertEqual(self.dev_with_addon[policy], self.dev_without_mariadb_policy[policy])
