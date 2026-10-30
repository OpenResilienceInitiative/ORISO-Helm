#!/usr/bin/env python3
"""Explicit chart job: activate only reviewed, already prepared native actors."""
import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from scenario_c_reconcile import ReconcileError, registry, migration_module
from scenario_c_activation import activate


def main():
    if (os.environ.get("TASK_IDENTITY_MODEL") != "FIVE_ACTORS"
            or os.environ.get("ORISO_SCENARIO_C_ACTIVATE") != "true"
            or os.environ.get("ORISO_SCENARIO_C_ISOLATED_REALM") != "true"):
        raise ReconcileError("Explicit isolated FIVE_ACTORS activation is required")
    actors = registry(ROOT / "scenario-c-task-identities.json")
    mapping = json.loads(os.environ["ORISO_ACTIVATION_MAPPING"])
    migration_module().validate_mapping(mapping)
    spec = importlib.util.spec_from_file_location("smtp_transport", ROOT / "keycloak-reconcile-smtp.py")
    smtp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smtp)
    url = smtp.base_url(os.environ["KEYCLOAK_URL"], "keycloak", os.environ["POD_NAMESPACE"])
    value = {"version": 1, "realmId": os.environ["KEYCLOAK_REALM_INTERNAL_ID"],
        "realmName": os.environ["ORISO_ACCOUNT_LIFECYCLE_REALM"],
        "issuer": os.environ["TASK_IDENTITY_ISSUER"],
        "contextKeyVersion": os.environ["ORISO_TASK_CONTEXT_KEY_VERSION"],
        "actors": {name: {"clientId": os.environ["IDENTITY_" + name + "_CLIENT_ID"],
            "serviceSubject": os.environ["IDENTITY_" + name + "_SERVICE_SUBJECT"]} for name in actors},
        "ordinaryGroups": json.loads(os.environ["ORISO_ACCOUNT_LIFECYCLE_GROUPS"]),
        "ordinaryPolicy": json.loads(os.environ["ORISO_ACCOUNT_MAINTENANCE_CONFIG"]),
        "protectedPolicy": json.loads(os.environ["ORISO_PROTECTED_INVITATIONS_CONFIG"]),
        "providerJarSha256": json.loads(os.environ["ORISO_PROVIDER_JAR_SHA256"])}
    admin = migration_module().Admin(url, value["realmName"], os.environ["ORISO_KEYCLOAK_INSTALLER_TOKEN"])
    changes = activate(admin, url, actors, mapping, value, os.environ)
    print(json.dumps({"actor_count": 5, "runtime_cutover": True, "changes": changes,
        "runtime_provider_bindings_verified": True, "legacy_retired": False}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never expose environment values, tokens, policy account IDs or exception payloads.
        print("SCENARIO_C_ACTIVATION_FAILED: inspect owned-job status and native binding inputs", file=sys.stderr)
        sys.exit(2)
