"""Render the opt-in Matrix PostgreSQL ingress rule against its chart workloads."""

import unittest

import yaml

from render_matrix_database_engine_test import database_block, render


POLICY = ("NetworkPolicy", "matrix-postgres-ingress")
NAMESPACE = "matrix-policy-test"
ENABLED = ("--set", "networkPolicies.matrixPostgresIngress.enabled=true")


class MatrixPostgresIngressPolicyTest(unittest.TestCase):
    def render_resources(self, *values):
        result = render("--namespace", NAMESPACE, *values)
        self.assertEqual(result.returncode, 0, result.stderr)
        resources = {
            (doc["kind"], doc["metadata"]["name"]): doc
            for doc in yaml.safe_load_all(result.stdout)
            if isinstance(doc, dict)
        }
        return resources, database_block(result.stdout)

    def test_no_policy_without_opt_in(self):
        for values in ((), ("--set-json", "networkPolicies=null")):
            with self.subTest(values=values):
                resources, _ = self.render_resources(*values)
                self.assertNotIn(POLICY, resources)

    def test_local_postgres_admits_only_synapse_on_the_database_port(self):
        engine = ("--set", "matrix.database.engine=postgres")
        baseline, original_database = self.render_resources(*engine)
        resources, configured_database = self.render_resources(*engine, *ENABLED)
        self.assertTrue(POLICY in resources, "Enabled Matrix PostgreSQL ingress policy is missing")
        policy = resources[POLICY]
        self.assertEqual(policy["metadata"]["namespace"], NAMESPACE)

        database = resources["StatefulSet", "matrix-postgres"]
        synapse = resources["Deployment", "matrix-synapse"]
        service = resources["Service", configured_database["args"]["host"]]
        database_label = database["spec"]["template"]["metadata"]["labels"]["app"]
        synapse_label = synapse["spec"]["template"]["metadata"]["labels"]["app"]
        [database_port] = service["spec"]["ports"]
        self.assertEqual(database_label, "matrix-postgres")
        self.assertEqual(synapse_label, "matrix-synapse")
        self.assertEqual(database_port["targetPort"], 5432)
        self.assertEqual(
            policy["spec"],
            {
                "podSelector": {"matchLabels": {"app": database_label}},
                "policyTypes": ["Ingress"],
                "ingress": [{
                    "from": [{"podSelector": {"matchLabels": {"app": synapse_label}}}],
                    "ports": [{"protocol": "TCP", "port": database_port["targetPort"]}],
                }],
            },
        )
        self.assertEqual(configured_database, original_database)
        self.assertEqual(resources.keys() - {POLICY}, baseline.keys())
        for key in baseline:
            if key[0] == "NetworkPolicy" or key in (
                ("StatefulSet", "matrix-postgres"), ("Deployment", "matrix-synapse"),
                ("Service", configured_database["args"]["host"]),
            ):
                self.assertEqual(resources[key], baseline[key], key)

    def test_opt_in_leaves_sqlite_and_external_postgres_configuration_intact(self):
        sqlite_resources, sqlite = self.render_resources(*ENABLED)
        self.assertTrue(POLICY in sqlite_resources, "Enabled Matrix PostgreSQL ingress policy is missing")
        self.assertEqual(sqlite, {"name": "sqlite3", "args": {"database": "/data/homeserver.db"}})

        external_host = "postgres.external.svc.cluster.local"
        external_resources, postgres = self.render_resources(
            *ENABLED, "--set", "matrix.database.engine=postgres",
            "--set-string", f"matrix.postgresHost={external_host}",
        )
        self.assertEqual(postgres["args"]["host"], external_host)
        self.assertEqual(external_resources[POLICY], sqlite_resources[POLICY])

    def test_nonboolean_enabled_value_fails(self):
        result = render("--set-string", "networkPolicies.matrixPostgresIngress.enabled=false")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("networkPolicies.matrixPostgresIngress.enabled must be a boolean", result.stderr)


if __name__ == "__main__":
    unittest.main()
