"""Render MongoDB ingress and the AgencyService and ConsultingTypeService port contracts."""

import subprocess
import unittest
from pathlib import Path

import yaml


CHART = Path(__file__).resolve().parents[1]
RELEASE = "mongo-policy"
NAMESPACE = "mongo-policy-test"
POLICY = ("NetworkPolicy", "mongodb-ingress")
MATRIXRTC_POLICIES = (
    ("NetworkPolicy", "matrixrtc-auth-policy-gateway"),
    ("NetworkPolicy", "matrixrtc-authorization-service"),
)


def render(*settings, dev=False):
    args = ["helm", "template", RELEASE, str(CHART), "--namespace", NAMESPACE]
    values = [
        "values.yaml.default",
        "secrets.yaml.default",
        "tests/fixtures/values-render-domain.yaml",
        "tests/fixtures/render-required-secrets.yaml",
    ]
    if dev:
        values.append("values-dev.yaml")
    for source in values:
        args.extend(["-f", str(CHART / source)])
    args.extend(["--set-string", "global.emailLegalOrganisationName=Render Test Foundation", *settings])
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    return {
        (document["kind"], document["metadata"]["name"]): document
        for document in yaml.safe_load_all(result.stdout)
        if document
    }


class NetworkPolicyScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.defaults = render()
        cls.omitted = render("--set-json", "networkPolicies=null")
        cls.dev_disabled = render("--set", "networkPolicies.mongodbIngress.enabled=false", dev=True)
        cls.enabled = render("--set", "networkPolicies.mongodbIngress.enabled=true")
        cls.dev_enabled = render("--set", "networkPolicies.mongodbIngress.enabled=true", dev=True)

    def test_policy_is_omitted_without_explicit_opt_in(self):
        for resources in (self.defaults, self.omitted, self.dev_disabled):
            self.assertFalse(POLICY in resources, "MongoDB ingress policy should require explicit opt-in")

    def assert_policy(self, resources, allowed_consumers):
        policy = resources.get(POLICY)
        self.assertIsNotNone(policy, "MongoDB ingress policy must render when enabled")
        self.assertEqual(policy["metadata"]["namespace"], NAMESPACE)
        spec = policy["spec"]
        self.assertEqual(spec["policyTypes"], ["Ingress"])
        self.assertEqual(spec["podSelector"], {"matchLabels": {"app": "mongodb"}})
        mongodb = resources["StatefulSet", "mongodb"]["spec"]["template"]
        self.assertEqual(mongodb["metadata"]["labels"]["app"], "mongodb")
        self.assertNotIn("egress", spec)

        [ingress] = spec["ingress"]
        [service_port] = resources["Service", "mongodb"]["spec"]["ports"]
        self.assertEqual(ingress["ports"], [{"protocol": "TCP", "port": service_port["targetPort"]}])
        expected = []
        for kind, name, labels in allowed_consumers:
            pod = resources[kind, name]["spec"]["template"]
            self.assertTrue(labels.items() <= pod["metadata"]["labels"].items(), name)
            expected.append({"podSelector": {"matchLabels": labels}})
        self.assertCountEqual(ingress["from"], expected)

    def test_opt_in_admits_only_the_application_and_bootstrap_pods(self):
        self.assert_policy(
            self.enabled,
            [
                ("Deployment", "agencyservice", {"app": "agencyservice"}),
                ("Deployment", "consultingtypeservice", {"app": "consultingtypeservice"}),
                ("Job", "create-mongo-users", {"app": "create-mongo-users"}),
            ],
        )

    def test_dev_opt_in_also_admits_the_monitoring_hook_and_cluster_collector(self):
        self.assert_policy(
            self.dev_enabled,
            [
                ("Deployment", "agencyservice", {"app": "agencyservice"}),
                ("Deployment", "consultingtypeservice", {"app": "consultingtypeservice"}),
                ("Job", "create-mongo-users", {"app": "create-mongo-users"}),
                ("Job", "db-monitoring-reconcile", {"app": "db-monitoring-reconcile"}),
                (
                    "Deployment",
                    f"{RELEASE}-k8s-infra-otel-deployment",
                    {
                        "app.kubernetes.io/name": "k8s-infra",
                        "app.kubernetes.io/instance": RELEASE,
                        "app.kubernetes.io/component": "otel-deployment",
                    },
                ),
            ],
        )

    def test_opt_in_does_not_change_matrixrtc_policies(self):
        for enabled, disabled in (
            (self.enabled, self.defaults),
            (self.dev_enabled, self.dev_disabled),
        ):
            for policy in MATRIXRTC_POLICIES:
                self.assertEqual(enabled[policy], disabled[policy])

    def test_agency_and_consulting_type_services_expose_only_http(self):
        for profile, resources in (("default", self.defaults), ("dev", self.dev_enabled)):
            for name, target_port in (("agencyservice", 8084), ("consultingtypeservice", 8083)):
                with self.subTest(profile=profile, service=name):
                    service = resources["Service", name]
                    self.assertEqual(
                        service["spec"]["ports"],
                        [{"name": "8080", "port": 8080, "targetPort": target_port}],
                    )
                    config_map = resources["ConfigMap", f"{name}-configmap-env"]
                    self.assertFalse("JAVA_OPTS" in config_map["data"])
                    deployment = resources["Deployment", name]
                    [container] = deployment["spec"]["template"]["spec"]["containers"]
                    self.assertFalse(any(entry["name"] == "JAVA_OPTS" for entry in container["env"]))
