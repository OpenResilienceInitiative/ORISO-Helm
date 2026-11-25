"""RUNTIME carries tenant-deletion to finish Träger deletions in TenantService (ORISO-Admin#1525).

UserService's nightly job lists the Träger in deletion and removes one once nothing references it.
Every receiver checks the exact RUNTIME role union, so the registry, both reconcilers, the receiver
fixture and the human-migration guard must agree on one set; TenantService requires the purpose
only behind tenantService.tenantDeletionFinishEnabled.
"""

import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(relative, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeTenantDeletionRoleTest(unittest.TestCase):
    def test_registry_declares_the_role_and_the_purpose_for_runtime(self):
        registry = json.loads((ROOT / "files/scenario-c-task-identities.json").read_text())
        runtime = next(actor for actor in registry["actors"] if actor["key"] == "RUNTIME")
        self.assertIn("tenant-deletion", runtime["roles"])
        self.assertIn("TENANT_DELETION", runtime["purposes"])

    def test_both_reconcilers_and_the_fixture_carry_the_role(self):
        clients = load("files/keycloak-reconcile-task-identities-clients.py", "task_clients_td")
        runtime = next(actor for actor in clients.ACTORS if actor[0] == "RUNTIME")
        self.assertIn("tenant-deletion", runtime[4])
        scenario = load("scripts/reconcile-keycloak-scenario-c.py", "scenario_c_td")
        self.assertIn("tenant-deletion", scenario.EXPECTED_ROLES["RUNTIME"])
        self.assertIn("TENANT_DELETION", scenario.EXPECTED_PURPOSES["RUNTIME"])
        fixture = load("tests/scenario_c_receiver_fixture.py", "receiver_fixture_td")
        fixture_runtime = next(actor for actor in fixture.ACTORS if actor[0] == "RUNTIME")
        self.assertIn("tenant-deletion", fixture_runtime[2])

    def test_human_group_migration_treats_the_role_as_machine_only(self):
        migration = load("scripts/migrate-keycloak-human-role-groups.py", "human_groups_td")
        self.assertIn("tenant-deletion", migration.TECHNICAL_ROLES)

    def test_tenantservice_requires_the_purpose_only_when_switched_on(self):
        # A TenantService image without the routes refuses to start when TENANT_DELETION is
        # required, so the chart must not require it before that image is deployed.
        configmap = (
            ROOT / "templates/tenantservice/tenantservice-configmap-env.yaml"
        ).read_text()
        self.assertIn('if eq (include "oriso.tenantDeletionFinishEnabled" .) "true"', configmap)
        self.assertIn('printf "%s,TENANT_DELETION" $requiredTasks', configmap)
        self.assertIn("TASK_IDENTITY_REQUIRED_TASKS: {{ $requiredTasks | quote }}", configmap)
        defaults = (ROOT / "values.yaml.default").read_text()
        self.assertIn("  tenantDeletionFinishEnabled: false\n", defaults)

    def test_tenantservice_pods_roll_when_its_env_configmap_changes(self):
        deployment = (
            ROOT / "templates/tenantservice/tenantservice-deployment.yaml"
        ).read_text()
        self.assertIn(
            'checksum/config-env: {{ include (print $.Template.BasePath '
            '"/tenantservice/tenantservice-configmap-env.yaml") . | sha256sum }}',
            deployment,
        )


if __name__ == "__main__":
    unittest.main()
