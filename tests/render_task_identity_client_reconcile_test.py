#!/usr/bin/env python3
"""Task identities client reconciliation must be an opt-in pre-upgrade hook."""
import pathlib
import subprocess

import yaml

from render_public_entry_rate_limit_test import CHART_DIR, VALUES

FIXTURE = pathlib.Path(CHART_DIR) / "tests/fixtures/task-identities-activation-values.yaml"


def render(*args):
    result = subprocess.run(
        ["helm", "template", "task-identities", CHART_DIR, *VALUES, "-f", str(FIXTURE), *args],
        capture_output=True,
        text=True,
    )
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc] if result.returncode == 0 else []
    return result, {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}


def environment(job):
    return {
        item["name"]: item
        for item in job["spec"]["template"]["spec"]["containers"][0]["env"]
    }


def main():
    provision, docs = render(
        "--set", "global.keycloak.taskIdentities.enabled=false",
        "--set", "global.keycloak.taskIdentities.provisionOnly=true",
        "--set-string", "global.keycloak.taskIdentities.setup.serviceSubject=",
        "--set-string", "global.keycloak.taskIdentities.accountCreate.serviceSubject=",
        "--set-string", "global.keycloak.taskIdentities.accountAdmin.serviceSubject=",
        "--set-string", "global.keycloak.taskIdentities.mail.serviceSubject=",
        "--set-string", "global.keycloak.taskIdentities.runtime.serviceSubject=",
    )
    assert provision.returncode == 0, provision.stderr
    job = docs[("Job", "keycloak-reconcile-task-identities-clients")]
    assert job["metadata"]["annotations"]["helm.sh/hook"] == "pre-upgrade"
    assert job["metadata"]["annotations"]["helm.sh/hook-delete-policy"] == "before-hook-creation"
    secret = docs[("Secret", "keycloak-reconcile-task-identities-clients")]
    assert secret["metadata"]["annotations"]["helm.sh/hook"] == "pre-upgrade"
    env = environment(job)
    assert env["TASK_IDENTITY_MODE"]["value"] == "provision"
    assert not any(name.endswith("SERVICE_SUBJECT") for name in env)
    for actor in ("SETUP", "ACCOUNT_CREATE", "ACCOUNT_ADMIN", "MAIL", "RUNTIME"):
        assert env[f"TASK_IDENTITY_{actor}_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"]["name"] == "keycloak-reconcile-task-identities-clients"

    enabled, docs = render()
    assert enabled.returncode == 0, enabled.stderr
    env = environment(docs[("Job", "keycloak-reconcile-task-identities-clients")])
    assert env["TASK_IDENTITY_MODE"]["value"] == "enable"
    for actor in ("SETUP", "ACCOUNT_CREATE", "ACCOUNT_ADMIN", "MAIL", "RUNTIME"):
        assert env[f"TASK_IDENTITY_{actor}_SERVICE_SUBJECT"]["value"]

    print("PASS: Task identities reconciler has explicit provision and enable modes")


if __name__ == "__main__":
    main()
