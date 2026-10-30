"""RUNTIME carries deletion-audit-read for the AgencyService deletion-log outbox (OBP 2.1 #130).

Every receiver and the UserService broker check the exact RUNTIME role union, so the registry,
both reconcilers, the receiver fixture and the human-migration guard must agree on one set.
"""

import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROLES = {
    "runtime-policy",
    "matrix-agency",
    "matrix-agency-provision",
    "appointment-sync",
    "appointment-participant-cleanup",
    "deletion-audit-read",
}
RUNTIME_AUDIENCES = {"tenantservice", "agencyservice", "appointmentservice"}


def load(relative, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeDeletionAuditRoleTest(unittest.TestCase):
    def test_registry_declares_the_role_and_the_outbox_purpose_for_runtime(self):
        registry = json.loads((ROOT / "files/scenario-c-task-identities.json").read_text())
        runtime = next(actor for actor in registry["actors"] if actor["key"] == "RUNTIME")
        self.assertEqual(RUNTIME_ROLES, set(runtime["roles"]))
        self.assertIn("DELETION_AUDIT_READ", runtime["purposes"])

    def test_client_reconciler_assigns_the_same_union_without_new_audiences(self):
        clients = load("files/keycloak-reconcile-task-identities-clients.py", "task_clients")
        runtime = next(actor for actor in clients.ACTORS if actor[0] == "RUNTIME")
        self.assertEqual(RUNTIME_ROLES, set(runtime[4]))
        self.assertEqual(RUNTIME_AUDIENCES, set(runtime[5]))

    def test_scenario_reconciler_expects_the_same_union_and_purpose(self):
        scenario = load("scripts/reconcile-keycloak-scenario-c.py", "scenario_c")
        self.assertEqual(RUNTIME_ROLES, scenario.EXPECTED_ROLES["RUNTIME"])
        self.assertIn("DELETION_AUDIT_READ", scenario.EXPECTED_PURPOSES["RUNTIME"])

    def test_receiver_fixture_issues_the_same_union(self):
        fixture = load("tests/scenario_c_receiver_fixture.py", "receiver_fixture")
        runtime = next(actor for actor in fixture.ACTORS if actor[0] == "RUNTIME")
        self.assertEqual(RUNTIME_ROLES, set(runtime[2]))
        self.assertEqual(RUNTIME_AUDIENCES, set(runtime[3]))

    def test_human_group_migration_treats_the_role_as_machine_only(self):
        migration = load("scripts/migrate-keycloak-human-role-groups.py", "human_groups")
        self.assertIn("deletion-audit-read", migration.TECHNICAL_ROLES)

    def test_agencyservice_requires_the_outbox_purpose(self):
        configmap = (
            ROOT / "templates/agencyservice/agencyservice-configmap-env.yaml"
        ).read_text()
        self.assertIn(
            'TASK_IDENTITY_REQUIRED_TASKS: '
            '"CONFIG_WIZARD,INVITE_RESERVATIONS,NOTIFICATION_DISPATCH,MATRIX_AGENCY,DELETION_AUDIT_READ"',
            configmap,
        )


if __name__ == "__main__":
    unittest.main()
