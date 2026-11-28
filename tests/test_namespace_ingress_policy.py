"""Namespace default-deny (Helm#538): every chart pod keeps the callers it has on Dev."""

import subprocess
import unittest
from pathlib import Path

import yaml

from render_addonservice_test import RENDER_ONLY_KEY


CHART = Path(__file__).resolve().parents[1]
RELEASE = "caritas"
NAMESPACE = "caritas"
CONTROLLER_NAMESPACE = "ingress-nginx"
CONTROLLER = {"app.kubernetes.io/name": "ingress-nginx", "app.kubernetes.io/component": "controller"}
STORE_POLICIES = (
    "mariadbIngress",
    "mongodbIngress",
    "rabbitmqIngress",
    "matrixPostgresIngress",
    "keycloakIngress",
    "clickhouseIngress",
)
ADDON = (
    "--set", "addonService.enabled=true",
    "--set-string", "global.secrets.addonServiceDbPassword=render-only-db-password",
    "--set-string", f"addonService.tokenPrivateKeyPem={RENDER_ONLY_KEY}",
)
def enabled(without=None):
    args = ["--set", "networkPolicies.namespaceIngress.enabled=true"]
    for name in STORE_POLICIES:
        args += ["--set", f"networkPolicies.{name}.enabled={str(name != without).lower()}"]
    # Dev's controller runs in its own namespace (Keycloak admits it only when named).
    return (*args, "--set", f"networkPolicies.keycloakIngress.externalNginxNamespace={CONTROLLER_NAMESPACE}")


ENABLED = enabled()
MATRIXRTC_POLICIES = ("matrixrtc-auth-policy-gateway", "matrixrtc-authorization-service")
NEW_POLICIES = {
    "namespace-default-deny-ingress",
    "application-ingress",
    "livekit-ingress",
    "redis-ingress",
    "ingress-controller-ingress",
    "zookeeper-ingress",
}
# The ClickHouse server pod is created by the operator, so it is not in the render.
CLICKHOUSE = {"app.kubernetes.io/name": "clickhouse", "app.kubernetes.io/instance": RELEASE, "app.kubernetes.io/component": "clickhouse"}


def render(*args, dev=True):
    command = ["helm", "template", RELEASE, str(CHART), "--namespace", NAMESPACE]
    sources = [
        "values.yaml.default",
        "secrets.yaml.default",
        "tests/fixtures/values-render-domain.yaml",
        "tests/fixtures/render-required-secrets.yaml",
    ]
    if dev:
        sources.append("values-dev.yaml")
    for source in sources:
        command.extend(["-f", str(CHART / source)])
    command.extend(["--set-string", "global.emailLegalOrganisationName=Render Test Foundation", *args])
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def policies(documents):
    return {d["metadata"]["name"]: d for d in documents if d["kind"] == "NetworkPolicy"}


def pods(documents):
    """Pod templates by workload name: (labels, container ports by name, hostNetwork)."""
    found = {}
    for document in documents:
        spec = document.get("spec") or {}
        if document["kind"] == "CronJob":
            spec = spec["jobTemplate"]["spec"]
        elif document["kind"] not in ("Deployment", "StatefulSet", "DaemonSet", "Job"):
            continue
        template = spec["template"]
        ports = {
            port["name"]: port["containerPort"]
            for container in template["spec"]["containers"]
            for port in container.get("ports", [])
            if "name" in port
        }
        found[document["metadata"]["name"]] = (
            template["metadata"].get("labels", {}),
            ports,
            bool(template["spec"].get("hostNetwork")),
        )
    return found


def selects(selector, labels):
    if not all(labels.get(key) == value for key, value in (selector.get("matchLabels") or {}).items()):
        return False
    for expression in selector.get("matchExpressions") or []:
        key, operator, values = expression["key"], expression["operator"], expression.get("values", [])
        if operator == "In" and labels.get(key) not in values:
            return False
        if operator == "NotIn" and key in labels and labels[key] in values:
            return False
        if operator == "Exists" and key not in labels:
            return False
        if operator == "DoesNotExist" and key in labels:
            return False
    return True


def peer_admits(peer, labels, namespace):
    if "ipBlock" in peer:
        return False
    if "namespaceSelector" in peer:
        if not selects(peer["namespaceSelector"], {"kubernetes.io/metadata.name": namespace}):
            return False
    elif namespace != NAMESPACE:
        return False
    return selects(peer.get("podSelector") or {}, labels)


def port_admits(rule, port, named_ports):
    if not rule.get("ports"):
        return True
    for allowed in rule["ports"]:
        value = allowed.get("port")
        if value is None or value == port or named_ports.get(value) == port:
            return True
    return False


def allowed(all_policies, target, port, client, namespace=NAMESPACE, named_ports=None):
    """True when any policy selecting `target` admits `client` on `port`."""
    named_ports = named_ports or {}
    for policy in all_policies.values():
        spec = policy["spec"]
        if "Ingress" not in spec.get("policyTypes", ["Ingress"]) or not selects(spec["podSelector"], target):
            continue
        for rule in spec.get("ingress") or []:
            sources = rule.get("from")
            if (not sources or any(peer_admits(peer, client, namespace) for peer in sources)) and port_admits(rule, port, named_ports):
                return True
    return False


class NamespaceIngressPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.documents = render(*ADDON, *ENABLED)
        cls.policies = policies(cls.documents)
        cls.pods = pods(cls.documents)
        cls.baseline = policies(render(*ADDON))

    def labels(self, workload):
        return self.pods[workload][0]

    def assert_allowed(self, target, port, *clients):
        labels, named_ports, _ = self.pods[target]
        for client in clients:
            client_labels = client if isinstance(client, dict) else self.labels(client)
            with self.subTest(target=target, port=port, client=client):
                self.assertTrue(allowed(self.policies, labels, port, client_labels, named_ports=named_ports))

    def test_off_by_default(self):
        self.assertFalse(NEW_POLICIES & set(policies(render(dev=False))))
        self.assertFalse(NEW_POLICIES & set(policies(render("--set", "networkPolicies.namespaceIngress.enabled=false"))))

    def test_dev_enables_the_default_deny(self):
        self.assertIn("namespace-default-deny-ingress", policies(render()))

    def test_requires_every_store_policy(self):
        for missing in STORE_POLICIES:
            with self.subTest(missing=missing), self.assertRaisesRegex(
                AssertionError, f"requires networkPolicies.{missing}.enabled=true"
            ):
                render(*enabled(without=missing))

    def test_rejects_a_non_boolean_switch(self):
        with self.assertRaisesRegex(AssertionError, "networkPolicies.namespaceIngress.enabled must be a boolean"):
            render("--set-string", "networkPolicies.namespaceIngress.enabled=true")

    def test_denies_by_default_for_the_whole_namespace(self):
        deny = self.policies["namespace-default-deny-ingress"]
        self.assertEqual(deny["metadata"]["namespace"], NAMESPACE)
        self.assertEqual(deny["spec"], {"podSelector": {}, "policyTypes": ["Ingress"]})

    def test_every_chart_pod_has_an_allow_rule(self):
        allow = {name: p for name, p in self.policies.items() if name != "namespace-default-deny-ingress"}
        for workload, (labels, _, host_network) in self.pods.items():
            if host_network:
                continue
            with self.subTest(workload=workload):
                self.assertTrue(
                    any(selects(p["spec"]["podSelector"], labels) and p["spec"].get("ingress") for p in allow.values()),
                    "pod would only match the default-deny",
                )

    def test_live_dev_connections_stay_allowed(self):
        # Seen on Dev in two /proc/net/tcp snapshots (26 Nov 2026) or set in the
        # rendered ConfigMaps and Job templates.
        self.assert_allowed("mariadb", 3306, "addonservice", "agencyservice", "consultingtypeservice", "keycloak", "tenantservice", "userservice")
        self.assert_allowed("mongodb", 27017, "agencyservice", "consultingtypeservice")
        self.assert_allowed("rabbitmq", 5672, "addonservice", "userservice")
        self.assert_allowed("redis", 6379, "userservice", "matrix-synapse", "matrixrtc-authorization-service")
        self.assert_allowed("matrix-postgres", 5432, "matrix-synapse")
        self.assert_allowed("matrix-synapse", 8008, "agencyservice", "userservice", "tenantservice")
        self.assert_allowed("keycloak", 8080, "addonservice", "agencyservice", "tenantservice", "userservice", "keycloak-reconcile-smtp")
        self.assert_allowed("userservice", 8082, "caritas-health-dashboard", "addonservice", "agencyservice", "tenantservice", "matrixrtc-auth-policy-gateway")
        self.assert_allowed("tenantservice", 8081, "caritas-health-dashboard", "userservice", "addonservice", "agencyservice", "consultingtypeservice")
        self.assert_allowed("consultingtypeservice", 8083, "userservice", "agencyservice", "tenantservice", "caritas-health-dashboard")
        self.assert_allowed("agencyservice", 8084, "caritas-health-dashboard", "userservice")
        self.assert_allowed(
            "caritas-signoz-otel-collector", 4318,
            "agencyservice", "consultingtypeservice", "tenantservice", "userservice",
            "caritas-k8s-infra-otel-agent", "caritas-k8s-infra-otel-deployment",
        )
        self.assert_allowed("caritas-signoz", 4320, "caritas-signoz-otel-collector")
        self.assert_allowed("caritas-signoz", 8080, "signoz-mcp-server", "caritas-signoz-observability")
        self.assert_allowed("caritas-zookeeper", 2181, CLICKHOUSE)
        self.assert_allowed("matrixrtc-auth-policy-gateway", 3010, "element-call")

    def test_redis_and_livekit_stay_open_to_node_addresses(self):
        node = {}  # hostNetwork LiveKit has no pod labels; an open rule admits it anyway
        for target in ("redis", "livekit"):
            [rule] = self.policies[f"{target}-ingress"]["spec"]["ingress"]
            self.assertEqual(rule, {})
            self.assertTrue(allowed(self.policies, self.labels(target), 6379, node, namespace="any"))

    def test_ingress_controller_reaches_every_ingress_backend(self):
        services = {d["metadata"]["name"]: d for d in self.documents if d["kind"] == "Service"}
        backends = set()
        for ingress in (d for d in self.documents if d["kind"] == "Ingress"):
            for rule in ingress["spec"].get("rules", []):
                for path in rule.get("http", {}).get("paths", []):
                    backends.add((path["backend"]["service"]["name"], path["backend"]["service"]["port"].get("number")))
        self.assertTrue(backends)
        for service_name, port in sorted(backends, key=str):
            service = services.get(service_name)
            if not service or not service["spec"].get("selector"):
                continue
            target_port = next(
                (p["targetPort"] for p in service["spec"]["ports"] if port is None or p["port"] == port),
                port,
            )
            targets = [w for w, (labels, _, hn) in self.pods.items() if not hn and selects({"matchLabels": service["spec"]["selector"]}, labels)]
            for workload in targets:
                labels, named_ports, _ = self.pods[workload]
                number = named_ports.get(target_port, target_port)
                with self.subTest(service=service_name, workload=workload, port=number):
                    self.assertTrue(allowed(self.policies, labels, number, CONTROLLER, namespace=CONTROLLER_NAMESPACE, named_ports=named_ports))

    def test_cert_manager_solver_pods_are_reachable_from_the_controller(self):
        solver = {"acme.cert-manager.io/http01-solver": "true"}
        self.assertTrue(allowed(self.policies, solver, 8089, CONTROLLER, namespace=CONTROLLER_NAMESPACE))

    def test_other_namespaces_are_blocked(self):
        for target, port in (("mariadb", 3306), ("userservice", 8082), ("keycloak", 8080), ("caritas-signoz", 8080), ("caritas-zookeeper", 2181)):
            with self.subTest(target=target):
                self.assertFalse(allowed(self.policies, self.labels(target), port, self.labels("userservice"), namespace="other"))

    def test_application_tier_leaves_stores_to_their_own_policies(self):
        tier = self.policies["application-ingress"]["spec"]["podSelector"]
        for workload in ("mariadb", "mongodb", "rabbitmq", "redis", "matrix-postgres", "keycloak", "caritas-zookeeper", *MATRIXRTC_POLICIES):
            with self.subTest(workload=workload):
                self.assertFalse(selects(tier, self.labels(workload)))
        clickhouse = self.policies[f"{RELEASE}-clickhouse-ingress"]["spec"]["podSelector"]["matchLabels"]
        self.assertEqual(clickhouse, CLICKHOUSE)
        self.assertFalse(selects(tier, clickhouse))
        self.assertTrue(selects(tier, {"acme.cert-manager.io/http01-solver": "true"}))

    def test_bundled_controller_policy_follows_the_controller(self):
        self.assertEqual(self.policies["ingress-controller-ingress"]["spec"]["ingress"], [{}])
        without = policies(render(*ADDON, *ENABLED, "--set", "nginxController.enabled=false"))
        self.assertNotIn("ingress-controller-ingress", without)

    def test_clickhouse_and_zookeeper_in_other_namespaces(self):
        away = ("--set-string", "signoz.clickhouse.namespace=telemetry")
        rendered = policies(render(*ADDON, *enabled(without="clickhouseIngress"), *away))
        self.assertIn("namespace-default-deny-ingress", rendered)
        [from_clickhouse, _] = rendered["zookeeper-ingress"]["spec"]["ingress"]
        self.assertEqual(from_clickhouse["from"][0]["namespaceSelector"], {"matchLabels": {"kubernetes.io/metadata.name": "telemetry"}})
        both_away = policies(render(*ADDON, *enabled(without="clickhouseIngress"), *away,
                                    "--set-string", "signoz.clickhouse.zookeeper.namespaceOverride=coordination"))
        self.assertNotIn("zookeeper-ingress", both_away)
        self.assertEqual(self.policies["zookeeper-ingress"]["spec"]["ingress"][0]["from"][0], {"podSelector": {"matchLabels": CLICKHOUSE}})

    def test_existing_matrixrtc_policies_are_unchanged(self):
        for name in MATRIXRTC_POLICIES:
            self.assertEqual(self.policies[name], self.baseline[name])


if __name__ == "__main__":
    unittest.main()
