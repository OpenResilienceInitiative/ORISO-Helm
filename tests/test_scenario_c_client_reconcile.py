"""Guard the Scenario C Keycloak client reconciler's non-secret input contract."""
import importlib.util
import os
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "scenario_c_client_reconcile", ROOT / "files/keycloak-reconcile-scenario-c-clients.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ScenarioCClientConfigurationTest(unittest.TestCase):
    def environment(self, mode="provision"):
        environment = {
            **os.environ,
            "KEYCLOAK_URL": "http://keycloak:8080/auth",
            "KEYCLOAK_REALM": "online-beratung",
            "POD_NAMESPACE": "caritas",
            "KEYCLOAK_ADMIN_USERNAME": "realmadmin",
            "KEYCLOAK_ADMIN_PASSWORD": "synthetic-master-password",
            "SCENARIO_C_MODE": mode,
        }
        for key, _, secret_name, subject_name, _, _ in MODULE.ACTORS:
            environment[secret_name] = "scenario-c-" + key.lower() + "-secret-012345678901234567890"
            if mode == "enable":
                environment[subject_name] = "00000000-0000-4000-8000-0000000000" + str(len(key))
        return environment

    def test_provision_accepts_distinct_secrets_without_subject_pins(self):
        config = MODULE.configuration(self.environment())
        self.assertEqual("provision", config["SCENARIO_C_MODE"])
        self.assertNotIn("SCENARIO_C_SETUP_SERVICE_SUBJECT", config)

    def test_enable_requires_distinct_subject_pins(self):
        environment = self.environment("enable")
        with self.assertRaises(MODULE.Error):
            MODULE.configuration(environment)

    def test_enable_accepts_distinct_subject_pins(self):
        environment = self.environment("enable")
        for index, (_, _, _, subject_name, _, _) in enumerate(MODULE.ACTORS, 10):
            environment[subject_name] = f"00000000-0000-4000-8000-{index:012d}"
        config = MODULE.configuration(environment)
        self.assertEqual("enable", config["SCENARIO_C_MODE"])

    def test_rejects_reused_actor_secret(self):
        environment = self.environment()
        environment["SCENARIO_C_RUNTIME_CLIENT_SECRET"] = environment["SCENARIO_C_SETUP_CLIENT_SECRET"]
        with self.assertRaises(MODULE.Error):
            MODULE.configuration(environment)


if __name__ == "__main__":
    unittest.main()
