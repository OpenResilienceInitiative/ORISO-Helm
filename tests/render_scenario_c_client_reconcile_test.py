#!/usr/bin/env python3
"""Scenario C client reconciliation must be an opt-in pre-upgrade hook."""
import pathlib
import subprocess

import yaml

from render_public_entry_rate_limit_test import CHART_DIR, VALUES

FIXTURE = pathlib.Path(CHART_DIR) / "tests/fixtures/scenario-c-activation-values.yaml"


def render(*args):
    result = subprocess.run(
        ["helm", "template", "scenario-c", CHART_DIR, *VALUES, "-f", str(FIXTURE), *args],
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
        "--set", "global.keycloak.scenarioC.enabled=false",
        "--set", "global.keycloak.scenarioC.provisionOnly=true",
        "--set-string", "global.keycloak.scenarioC.setup.serviceSubject=",
        "--set-string", "global.keycloak.scenarioC.accountCreate.serviceSubject=",
        "--set-string", "global.keycloak.scenarioC.accountAdmin.serviceSubject=",
        "--set-string", "global.keycloak.scenarioC.mail.serviceSubject=",
        "--set-string", "global.keycloak.scenarioC.runtime.serviceSubject=",
    )
    assert provision.returncode == 0, provision.stderr
    job = docs[("Job", "keycloak-reconcile-scenario-c-clients")]
    assert job["metadata"]["annotations"]["helm.sh/hook"] == "pre-upgrade"
    env = environment(job)
    assert env["SCENARIO_C_MODE"]["value"] == "provision"
    assert not any(name.endswith("SERVICE_SUBJECT") for name in env)
    for actor in ("SETUP", "ACCOUNT_CREATE", "ACCOUNT_ADMIN", "MAIL", "RUNTIME"):
        assert env[f"SCENARIO_C_{actor}_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"]["name"] == "keycloak-backend-client-secrets"

    enabled, docs = render()
    assert enabled.returncode == 0, enabled.stderr
    env = environment(docs[("Job", "keycloak-reconcile-scenario-c-clients")])
    assert env["SCENARIO_C_MODE"]["value"] == "enable"
    for actor in ("SETUP", "ACCOUNT_CREATE", "ACCOUNT_ADMIN", "MAIL", "RUNTIME"):
        assert env[f"SCENARIO_C_{actor}_SERVICE_SUBJECT"]["value"]

    print("PASS: Scenario C reconciler has explicit provision and enable modes")


if __name__ == "__main__":
    main()
