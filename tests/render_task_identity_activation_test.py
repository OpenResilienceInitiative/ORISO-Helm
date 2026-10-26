#!/usr/bin/env python3
"""Task identities must be an explicit all-or-nothing UserService cutover."""
import pathlib
import subprocess

import yaml

from render_public_entry_rate_limit_test import CHART_DIR, VALUES

FIXTURE = pathlib.Path(CHART_DIR) / "tests/fixtures/task-identities-activation-values.yaml"
ACTORS = ("SETUP", "ACCOUNT_CREATE", "ACCOUNT_ADMIN", "MAIL", "RUNTIME")
RECEIVERS = {
    "consultingtypeservice": {
        "tasks": "CONFIG_WIZARD,SMTP_SYNC,SYSTEM_EMAIL_DELIVERY",
        "audience": "consultingtypeservice",
        "actors": ("SETUP", "MAIL"),
    },
    "tenantservice": {
        "tasks": "CONFIG_WIZARD,INVITE_RESERVATIONS,NOTIFICATION_DISPATCH,RUNTIME_POLICY",
        "audience": "tenantservice",
        "actors": ("SETUP", "MAIL", "RUNTIME"),
    },
    "agencyservice": {
        "tasks": "CONFIG_WIZARD,INVITE_RESERVATIONS,NOTIFICATION_DISPATCH,MATRIX_AGENCY",
        "audience": "agencyservice",
        "actors": ("SETUP", "MAIL", "RUNTIME"),
    },
}


def render(*args, task_identity=True):
    values = [*VALUES]
    if task_identity:
        values.extend(("-f", str(FIXTURE)))
    result = subprocess.run(
        ["helm", "template", "task-identities", CHART_DIR, *values, *args],
        capture_output=True,
        text=True,
    )
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc] if result.returncode == 0 else []
    return result, {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}


def env_by_name(deployment):
    return {
        item["name"]: item
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }


def main():
    legacy, legacy_docs = render(task_identity=False)
    assert legacy.returncode == 0, legacy.stderr
    legacy_config = legacy_docs[("ConfigMap", "userservice-configmap-env")]["data"]
    legacy_secret = legacy_docs[("Secret", "keycloak-backend-client-secrets")]["data"]
    legacy_env = env_by_name(legacy_docs[("Deployment", "userservice")])
    assert legacy_config["TASK_IDENTITY_MODEL"] == "LEGACY"
    for actor in ACTORS:
        assert f"IDENTITY_{actor}_CLIENT_ID" not in legacy_config
        assert f"IDENTITY_{actor}_CLIENT_SECRET" not in legacy_secret
        assert f"IDENTITY_{actor}_CLIENT_ID" not in legacy_env
    for service, receiver in RECEIVERS.items():
        receiver_config = legacy_docs[("ConfigMap", f"{service}-configmap-env")]["data"]
        receiver_env = env_by_name(legacy_docs[("Deployment", service)])
        assert "TASK_IDENTITY_MODEL" not in receiver_config
        assert "TASK_IDENTITY_MODEL" not in receiver_env
        for actor in receiver["actors"]:
            assert f"IDENTITY_{actor}_CLIENT_ID" not in receiver_config
            assert f"IDENTITY_{actor}_CLIENT_ID" not in receiver_env

    enabled, docs = render()
    assert enabled.returncode == 0, enabled.stderr
    config = docs[("ConfigMap", "userservice-configmap-env")]["data"]
    secret = docs[("Secret", "keycloak-backend-client-secrets")]["data"]
    env = env_by_name(docs[("Deployment", "userservice")])
    context_secret = docs[("Secret", "task-identity-context-secrets")]["data"]

    assert config["TASK_IDENTITY_MODEL"] == "FIVE_ACTORS"
    for actor in ACTORS:
        assert config[f"IDENTITY_{actor}_CLIENT_ID"]
        assert config[f"IDENTITY_{actor}_SERVICE_SUBJECT"]
        assert f"IDENTITY_{actor}_CLIENT_SECRET" in secret
        assert env[f"IDENTITY_{actor}_CLIENT_ID"]["valueFrom"]["configMapKeyRef"] == {
            "name": "userservice-configmap-env", "key": f"IDENTITY_{actor}_CLIENT_ID"
        }
        assert env[f"IDENTITY_{actor}_SERVICE_SUBJECT"]["valueFrom"]["configMapKeyRef"] == {
            "name": "userservice-configmap-env", "key": f"IDENTITY_{actor}_SERVICE_SUBJECT"
        }
        assert env[f"IDENTITY_{actor}_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"] == {
            "name": "keycloak-backend-client-secrets", "key": f"IDENTITY_{actor}_CLIENT_SECRET"
        }

    for service, receiver in RECEIVERS.items():
        receiver_config = docs[("ConfigMap", f"{service}-configmap-env")]["data"]
        receiver_env = env_by_name(docs[("Deployment", service)])
        assert receiver_config["TASK_IDENTITY_MODEL"] == "FIVE_ACTORS"
        assert receiver_config["TASK_IDENTITY_REQUIRED_TASKS"] == receiver["tasks"]
        assert receiver_config["TASK_IDENTITY_AUDIENCE"] == receiver["audience"]
        for key in ("TASK_IDENTITY_MODEL", "TASK_IDENTITY_REQUIRED_TASKS", "TASK_IDENTITY_AUDIENCE"):
            assert receiver_env[key]["valueFrom"]["configMapKeyRef"] == {
                "name": f"{service}-configmap-env", "key": key
            }
        for actor in receiver["actors"]:
            for suffix in ("CLIENT_ID", "SERVICE_SUBJECT"):
                key = f"IDENTITY_{actor}_{suffix}"
                assert receiver_config[key] == config[key]
                assert receiver_env[key]["valueFrom"]["configMapKeyRef"] == {
                    "name": f"{service}-configmap-env", "key": key
                }
        assert not any(
            item["name"] == f"IDENTITY_{actor}_CLIENT_SECRET"
            for item in receiver_env.values()
            for actor in ACTORS
        )

    assert set(context_secret) == {
        "ORISO_TENANT_CREATION_CONTEXT_KEY",
        "ORISO_WIZARD_POLICY_CONTEXT_KEY",
    }
    tenant_env = env_by_name(docs[("Deployment", "tenantservice")])
    assert tenant_env["ORISO_TENANT_CREATION_CONTEXT_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "task-identity-context-secrets", "key": "ORISO_TENANT_CREATION_CONTEXT_KEY"
    }
    assert tenant_env["ORISO_WIZARD_POLICY_CONTEXT_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "task-identity-context-secrets", "key": "ORISO_WIZARD_POLICY_CONTEXT_KEY"
    }
    cts_env = env_by_name(docs[("Deployment", "consultingtypeservice")])
    assert cts_env["ORISO_TENANT_CREATION_CONTEXT_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "task-identity-context-secrets", "key": "ORISO_TENANT_CREATION_CONTEXT_KEY"
    }
    assert "ORISO_WIZARD_POLICY_CONTEXT_KEY" not in cts_env
    agency_env = env_by_name(docs[("Deployment", "agencyservice")])
    assert "ORISO_TENANT_CREATION_CONTEXT_KEY" not in agency_env

    missing, _ = render("--set-string", "global.keycloak.taskIdentities.setup.serviceSubject=")
    assert missing.returncode != 0, "missing actor subject must fail rendering"
    missing_context, _ = render(
        "--set-string", "global.secrets.taskIdentityTenantCreationContextKey="
    )
    assert missing_context.returncode != 0, "missing tenant context key must fail rendering"
    duplicate, _ = render(
        "--set-string",
        "global.keycloak.taskIdentities.runtime.serviceSubject=00000000-0000-4000-8000-000000000010",
    )
    assert duplicate.returncode != 0, "duplicate actor subject must fail rendering"
    reused_secret, _ = render(
        "--set-string",
        "global.secrets.keycloakTaskIdentityRuntimeClientSecret=render-only-task-identities-setup-secret-canary",
    )
    assert reused_secret.returncode != 0, "reused actor secret must fail rendering"
    bad_client, _ = render(
        "--set-string", "global.keycloak.taskIdentities.mail.clientId=backend-technical"
    )
    assert bad_client.returncode != 0, "non-canonical actor client must fail rendering"

    provision, provision_docs = render(
        "--set", "global.keycloak.taskIdentities.enabled=false",
        "--set", "global.keycloak.taskIdentities.provisionOnly=true",
        "--set-string", "global.keycloak.taskIdentities.setup.serviceSubject=",
        "--set-string", "global.keycloak.taskIdentities.accountCreate.serviceSubject=",
        "--set-string", "global.keycloak.taskIdentities.accountAdmin.serviceSubject=",
        "--set-string", "global.keycloak.taskIdentities.mail.serviceSubject=",
        "--set-string", "global.keycloak.taskIdentities.runtime.serviceSubject=",
    )
    assert provision.returncode == 0, provision.stderr
    provision_config = provision_docs[("ConfigMap", "userservice-configmap-env")]["data"]
    provision_env = env_by_name(provision_docs[("Deployment", "userservice")])
    assert provision_config["TASK_IDENTITY_MODEL"] == "LEGACY"
    assert "IDENTITY_SETUP_CLIENT_ID" not in provision_env

    conflicting, _ = render("--set", "global.keycloak.taskIdentities.provisionOnly=true")
    assert conflicting.returncode != 0, "enable and provisionOnly must be mutually exclusive"

    print("PASS: task-identity activation renders five complete, distinct actor bindings")


if __name__ == "__main__":
    main()
