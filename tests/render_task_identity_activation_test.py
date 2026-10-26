#!/usr/bin/env python3
"""Task identities must be an explicit all-or-nothing UserService cutover."""
import pathlib
import subprocess

import yaml

from render_public_entry_rate_limit_test import CHART_DIR, VALUES

FIXTURE = pathlib.Path(CHART_DIR) / "tests/fixtures/task-identities-activation-values.yaml"
ACTORS = ("SETUP", "ACCOUNT_CREATE", "ACCOUNT_ADMIN", "MAIL", "RUNTIME")


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

    enabled, docs = render()
    assert enabled.returncode == 0, enabled.stderr
    config = docs[("ConfigMap", "userservice-configmap-env")]["data"]
    secret = docs[("Secret", "keycloak-backend-client-secrets")]["data"]
    env = env_by_name(docs[("Deployment", "userservice")])

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

    missing, _ = render("--set-string", "global.keycloak.taskIdentities.setup.serviceSubject=")
    assert missing.returncode != 0, "missing actor subject must fail rendering"
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
