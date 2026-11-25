"""The Keycloak startup gate renders on the private management interface."""

import unittest

from render_signoz_dev_observability_test import by_key, env, render


SUBCHART = "online-counseling-keycloak"
PROBE_VALUES = f"{SUBCHART}.keycloakProbes"
DEPLOYMENT = ("Deployment", "keycloak")
SERVICE = ("Service", "keycloak")
PROBES = {"startupProbe": "started", "livenessProbe": "live", "readinessProbe": "ready"}


def resources(*overlays, args=()):
    return by_key(render(*overlays, helm_args=args))


def container(chart):
    return chart[DEPLOYMENT]["spec"]["template"]["spec"]["containers"][0]


class KeycloakReadinessTest(unittest.TestCase):
    def test_dev_health_and_database_checks_gate_traffic_without_routing_management(self):
        dev = resources("values-dev.yaml")
        off = resources("values-dev.yaml", args=("--set", f"{PROBE_VALUES}.useManagementPort=false"))
        keycloak = container(dev)
        self.assertIn({"name": "management", "containerPort": 9000}, keycloak["ports"])
        settings = env(dev[DEPLOYMENT])
        self.assertEqual(settings["KC_HEALTH_ENABLED"], "true")
        self.assertEqual(settings["KC_METRICS_ENABLED"], "true")
        for probe, path in PROBES.items():
            self.assertEqual(keycloak[probe]["httpGet"], {
                "path": f"/auth/health/{path}", "port": "management",
            })
        self.assertEqual(dev[SERVICE], off[SERVICE])
        self.assertEqual(
            {key: doc for key, doc in dev.items() if key[0] == "Ingress"},
            {key: doc for key, doc in off.items() if key[0] == "Ingress"},
        )

    def test_management_health_works_with_metrics_scraping_off(self):
        chart = resources("values-dev.yaml", args=("--set", "global.observability.appMetrics=false"))
        keycloak = container(chart)
        self.assertIn({"name": "management", "containerPort": 9000}, keycloak["ports"])
        settings = env(chart[DEPLOYMENT])
        self.assertEqual(settings["KC_HEALTH_ENABLED"], "true")
        self.assertEqual(settings["KC_METRICS_ENABLED"], "true")
        self.assertNotIn("KC_EVENT_METRICS_USER_ENABLED", settings)
        annotations = chart[DEPLOYMENT]["spec"]["template"]["metadata"]["annotations"]
        self.assertFalse(any(key.startswith("prometheus.io/") for key in annotations))

    def test_base_and_layered_pre_dev_keep_the_old_default(self):
        for overlays in ((), ("values-dev.yaml", "values-pre-dev.yaml")):
            with self.subTest(overlays=overlays):
                chart = resources(*overlays)
                self.assertNotIn("KC_HEALTH_ENABLED", env(chart[DEPLOYMENT]))
                self.assertTrue(PROBES.keys().isdisjoint(container(chart)))

    def test_multiple_replicas_use_private_health_and_jdbc_discovery(self):
        default = resources(args=("--set", "networkPolicies.keycloakIngress.enabled=true"))
        clustered = resources(args=(
            "--set", "networkPolicies.keycloakIngress.enabled=true",
            "--set", f"{SUBCHART}.replicas=2",
        ))
        pdb = ("PodDisruptionBudget", "keycloak")
        self.assertNotIn(pdb, default)
        self.assertEqual(default[DEPLOYMENT]["spec"]["replicas"], 1)
        self.assertNotIn("KC_CACHE_STACK", env(default[DEPLOYMENT]))
        self.assertEqual(clustered[DEPLOYMENT]["spec"]["replicas"], 2)
        settings = env(clustered[DEPLOYMENT])
        self.assertEqual(settings["KC_CACHE"], "ispn")
        self.assertEqual(settings["KC_CACHE_STACK"], "jdbc-ping")
        self.assertEqual(settings["KC_HEALTH_ENABLED"], "true")
        self.assertEqual(settings["KC_METRICS_ENABLED"], "true")
        keycloak = container(clustered)
        self.assertEqual(keycloak["readinessProbe"]["httpGet"], {
            "path": "/auth/health/ready", "port": "management",
        })
        self.assertEqual({port.get("name"): port["containerPort"] for port in keycloak["ports"]}, {
            None: 8080, "management": 9000, "cache-transport": 7800, "cache-failure": 57800,
        })
        pod = clustered[DEPLOYMENT]["spec"]["template"]
        anti = pod["spec"]["affinity"]["podAntiAffinity"]
        self.assertNotIn("requiredDuringSchedulingIgnoredDuringExecution", anti)
        (preferred,) = anti["preferredDuringSchedulingIgnoredDuringExecution"]
        self.assertEqual(preferred["podAffinityTerm"], {
            "topologyKey": "kubernetes.io/hostname", "labelSelector": {"matchLabels": pod["metadata"]["labels"]},
        })
        self.assertEqual(clustered[pdb]["spec"], {
            "maxUnavailable": 1, "selector": {"matchLabels": pod["metadata"]["labels"]},
        })
        self.assertEqual(clustered[SERVICE], default[SERVICE])
        policy = ("NetworkPolicy", "keycloak-ingress")
        self.assertEqual(clustered[policy], default[policy])
        self.assertEqual(
            {key: doc for key, doc in clustered.items() if key[0] == "Ingress"},
            {key: doc for key, doc in default.items() if key[0] == "Ingress"},
        )

    def test_invalid_replica_counts_cannot_disable_the_deployment(self):
        for replicas in ("0", "-1", "2.5", "invalid", "2147483648"):
            with self.subTest(replicas=replicas), self.assertRaisesRegex(
                AssertionError, "online-counseling-keycloak.replicas must be a positive 32-bit integer"
            ):
                resources(args=("--set-string", f"{SUBCHART}.replicas={replicas}"))

    def test_existing_app_port_path_and_timing_overrides_still_render(self):
        chart = resources(args=(
            "--set-string", f"{PROBE_VALUES}.startup.path=/custom/started",
            "--set-string", f"{PROBE_VALUES}.liveness.path=/custom/live",
            "--set-string", f"{PROBE_VALUES}.readiness.path=/custom/ready",
            "--set", f"{PROBE_VALUES}.readiness.periodSeconds=7",
        ))
        keycloak = container(chart)
        self.assertNotIn("KC_HEALTH_ENABLED", env(chart[DEPLOYMENT]))
        self.assertEqual(keycloak["readinessProbe"]["periodSeconds"], 7)
        for probe, path in PROBES.items():
            self.assertEqual(keycloak[probe]["httpGet"], {"path": f"/custom/{path}", "port": 8080})


if __name__ == "__main__":
    unittest.main()
