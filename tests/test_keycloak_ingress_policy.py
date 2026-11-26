"""Verify the opt-in Keycloak policy against the rendered ORISO chart."""

import unittest

from render_addonservice_test import RENDER_ONLY_KEY
from render_signoz_dev_observability_test import by_key, render
from render_task_identity_activation_test import render as render_task_identities


RELEASE = "keycloak-policy"
TASK_HOOK = ("Job", "keycloak-reconcile-task-identities-clients")
POLICY = ("NetworkPolicy", "keycloak-ingress")
ENABLED = ("--set", "networkPolicies.keycloakIngress.enabled=true")
# values-dev.yaml enables the policy for the external controller (#538).
DISABLED = ("--set", "networkPolicies.keycloakIngress.enabled=false", "--set", "networkPolicies.namespaceIngress.enabled=false")
NO_EXTERNAL = ("--set-string", "networkPolicies.keycloakIngress.externalNginxNamespace=")
EXTERNAL = (
    "--set", "nginxController.enabled=false",
    "--set", "networkPolicies.keycloakIngress.externalNginxNamespace=ingress-nginx",
)
NGINX = {"app.kubernetes.io/name": "ingress-nginx", "app.kubernetes.io/component": "controller"}


def resources(*overlays, args=()):
    return by_key(render(*overlays, release=RELEASE, helm_args=args))


def pod_labels(workload):
    spec = workload["spec"]
    if workload["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"]["metadata"]["labels"]


def tcp(*ports):
    return [{"protocol": "TCP", "port": port} for port in ports]


class KeycloakIngressPolicyTest(unittest.TestCase):
    def test_default_does_not_isolate_keycloak(self):
        self.assertNotIn(POLICY, resources())
        self.assertNotIn(POLICY, resources("values-dev.yaml", args=DISABLED))
        self.assertNotIn(POLICY, resources(args=("--set", "nginxController.enabled=false")))

    def test_bundled_controller_and_all_chart_clients_use_the_public_http_port(self):
        profile = ("values-dev.yaml",)
        flags = (
            "--set", "addonService.enabled=true",
            "--set-string", "global.secrets.addonServiceDbPassword=render-only-db-password",
            "--set-string", f"addonService.tokenPrivateKeyPem={RENDER_ONLY_KEY}",
            *NO_EXTERNAL,
        )
        before = resources(*profile, args=(*flags, *DISABLED))
        # Compare with this policy alone: DISABLED also switches off the namespace default-deny.
        after = resources(*profile, args=(*flags, *ENABLED, *DISABLED[2:]))
        policy = after.pop(POLICY)
        self.assertEqual(set(after), set(before))
        self.assertEqual(policy["metadata"]["namespace"], "caritas")
        self.assertEqual(set(policy["spec"]), {"podSelector", "policyTypes", "ingress"})
        self.assertEqual(policy["spec"]["policyTypes"], ["Ingress"])

        backend = ("Deployment", "keycloak")
        self.assertEqual(after[backend], before[backend])
        keycloak = {"matchLabels": pod_labels(after[backend])}
        self.assertEqual(policy["spec"]["podSelector"], keycloak)
        service = ("Service", "keycloak")
        self.assertEqual(after[service], before[service])
        http = next(port["targetPort"] for port in after[service]["spec"]["ports"] if port["name"] == "http")
        self.assertEqual(http, 8080)

        controller = pod_labels(after["Deployment", "ingress-nginx-controller"])
        self.assertEqual(controller, {**NGINX, "app.kubernetes.io/instance": "ingress-nginx"})
        callers = {
            ("Deployment", "addonservice"),
            ("Deployment", "agencyservice"),
            ("Deployment", "consultingtypeservice"),
            ("Deployment", "tenantservice"),
            ("Deployment", "userservice"),
            ("Job", "keycloak-bootstrap-users"),
            ("Job", "keycloak-reconcile-mail-locales"),
            ("Job", "keycloak-reconcile-service-identities"),
            ("Job", "keycloak-reconcile-smtp"),
            ("CronJob", "keycloak-reconcile-smtp"),
            ("Job", "keycloak-verify-2fa-contract"),
        }
        expected_app_labels = {pod_labels(after[k])["app"] for k in callers} | {TASK_HOOK[1]}
        web, cache, metrics = policy["spec"]["ingress"]
        self.assertEqual(web["ports"], tcp(http))
        self.assertEqual(web["from"][0], {"podSelector": {"matchLabels": controller}})
        selector = web["from"][1]["podSelector"]
        (expression,) = selector["matchExpressions"]
        self.assertEqual((expression["key"], expression["operator"]), ("app", "In"))
        self.assertEqual(set(expression["values"]), expected_app_labels)
        self.assertEqual(len(web["from"]), 2)
        self.assertEqual(cache, {"from": [{"podSelector": keycloak}], "ports": tcp(7800, 57800)})

        management = next(port["containerPort"] for port in after[backend]["spec"]["template"]["spec"]["containers"][0]["ports"] if port.get("name") == "management")
        collector = pod_labels(after["Deployment", f"{RELEASE}-k8s-infra-otel-deployment"])
        self.assertEqual(metrics, {"from": [{"podSelector": {"matchLabels": collector}}], "ports": tcp(management)})

    def test_existing_policy_admits_the_first_task_identity_pre_upgrade_hook(self):
        args = (*ENABLED, "--namespace", "caritas")
        result, base = render_task_identities(*args, task_identity=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        base_from = base[POLICY]["spec"]["ingress"][0]["from"]
        base_callers = base_from[-1]["podSelector"]["matchExpressions"][0]["values"]
        result, chart = render_task_identities(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(TASK_HOOK, base)
        task = chart[TASK_HOOK]
        self.assertEqual(task["metadata"]["annotations"]["helm.sh/hook"], "pre-upgrade")
        task_pod = pod_labels(task)["app"]
        callers = chart[POLICY]["spec"]["ingress"][0]["from"][-1]["podSelector"]["matchExpressions"][0]["values"]
        self.assertIn(task_pod, base_callers)
        self.assertEqual(set(callers), set(base_callers))

    def test_external_controller_is_restricted_to_the_explicit_namespace_and_pods(self):
        # Dev's own values supply the policy and the external namespace.
        chart = resources("values-dev.yaml", args=EXTERNAL[:2])
        self.assertNotIn(("Deployment", "ingress-nginx-controller"), chart)
        web = chart[POLICY]["spec"]["ingress"][0]
        self.assertEqual(web["from"][0], {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "ingress-nginx"}},
            "podSelector": {"matchLabels": NGINX},
        })
        self.assertEqual(len(web["from"]), 2)

    def test_metrics_rule_is_absent_without_the_scraper(self):
        for overlay, args in (
            ((), ENABLED),
            (("values-dev.yaml",), (*ENABLED, "--set", "global.observability.appMetrics=false")),
        ):
            with self.subTest(overlay=overlay, args=args):
                policy = resources(*overlay, args=args)[POLICY]
                self.assertEqual(len(policy["spec"]["ingress"]), 2)

    def test_enabled_without_a_controller_source_is_rejected(self):
        for overlays in ((), ("values-dev.yaml",)):
            with self.subTest(overlays=overlays):
                with self.assertRaisesRegex(AssertionError, "requires the bundled ingress-nginx controller or an externalNginxNamespace"):
                    resources(*overlays, args=(*ENABLED, *NO_EXTERNAL, "--set", "nginxController.enabled=false"))

    def test_bad_policy_settings_fail_rendering(self):
        with self.assertRaisesRegex(AssertionError, "networkPolicies.keycloakIngress.enabled must be a boolean"):
            resources(args=("--set-string", "networkPolicies.keycloakIngress.enabled=false"))
        with self.assertRaisesRegex(AssertionError, "externalNginxNamespace must be a Kubernetes namespace name"):
            resources(args=(*ENABLED, "--set-string", "networkPolicies.keycloakIngress.externalNginxNamespace=bad.namespace"))


if __name__ == "__main__":
    unittest.main()
