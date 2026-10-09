#!/usr/bin/env python3
"""Break-glass separation (Helm#422): master bootstrap admin vs. ORISO-realm realmadmin."""
import base64
import json
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE = ["helm", "template", "break-glass", str(ROOT)]
for name in ("values.yaml.default", "tests/fixtures/values-render-domain.yaml",
             "secrets.yaml.default", "tests/fixtures/render-required-secrets.yaml"):
    BASE += ["-f", str(ROOT / name)]
BASE += ["--set-string", "global.secrets.redisdefaultPass=test-redis-password"]


def render(*extra):
    result = subprocess.run(BASE + list(extra), capture_output=True, text=True)
    docs = [d for d in yaml.safe_load_all(result.stdout) if d] if result.returncode == 0 else []
    return result, {(d["kind"], d["metadata"]["name"]): d for d in docs}


def check_fresh_import_disables_realm_admin(docs):
    realm = json.loads(docs[("ConfigMap", "keycloak-configmap-data")]["data"]["realm.json"])
    user = next(u for u in realm["users"] if u["username"] == "realmadmin")
    assert user["enabled"] is False, user
    assert not user.get("credentials"), "realmadmin must not ship a credential"


def check_no_automation_uses_realm_admin(docs):
    secret = docs[("Secret", "keycloak-secret-env")]["data"]
    master = base64.b64decode(secret["KEYCLOAK_ADMIN"]).decode()
    assert master != "realmadmin", "fresh installs get a master admin distinct from the realm user"
    bootstrap = docs[("Job", "keycloak-bootstrap-users")]["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e for e in bootstrap["env"]}
    assert env["REALM_ADMIN_USERNAME"] == {"name": "REALM_ADMIN_USERNAME", "value": "realmadmin"}
    assert "REALM_ADMIN_PASSWORD" not in env
    script = bootstrap["command"][-1]
    assert "set-password" not in script and '"enabled": false' in script
    assert '--rolename "technical"' not in script
    for (kind, name), doc in docs.items():
        if kind not in ("Job", "CronJob", "Deployment", "StatefulSet", "DaemonSet"):
            continue
        text = json.dumps(doc)
        # Every scripted admin login targets master; nothing logs in to the ORISO realm as a person.
        assert "--realm \\\"$KEYCLOAK_REALM\\\"" not in text and "grant_type=password" not in text, name
        for container in doc_containers(doc):
            for e in container.get("env", []):
                ref = e.get("valueFrom", {}).get("secretKeyRef", {})
                if ref.get("name") == "keycloak-secret-env" and ref.get("key", "").startswith("KEYCLOAK_ADMIN"):
                    assert not e["name"].startswith("REALM_ADMIN"), (name, e["name"])


def doc_containers(doc):
    spec = doc["spec"]
    if doc["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"]["spec"].get("containers", [])


def check_installer_chooses_master_name(docs):
    shipped = yaml.safe_load((ROOT / "secrets.yaml.default").read_text())
    assert shipped["global"]["secrets"]["keycloakAdminUsername"] == "", "no shipped master admin name"
    secret = docs[("Secret", "keycloak-secret-env")]["data"]
    assert base64.b64decode(secret["KEYCLOAK_ADMIN"]).decode() == "bootstrap-admin"


def name(value):
    return ("--set-string", "global.secrets.keycloakAdminUsername=" + value)


def check_install_rejects_missing_or_well_known_name():
    result, _ = render(*name(""))
    assert result.returncode != 0 and "keycloakAdminUsername is required" in result.stderr, result.stderr
    for value in ("admin", "realmadmin", "keycloak", "root", "Admin", "REALMADMIN", "KeyCloak", "Root"):
        result, _ = render(*name(value))
        assert result.returncode != 0 and "well-known name" in result.stderr, (value, result.stderr)
    custom = ("--set-string", "global.keycloak.bootstrapUsers.realmAdmin.username=ops-admin")
    result, _ = render(*name("Ops-Admin"), *custom)
    assert result.returncode != 0 and "must differ from" in result.stderr, result.stderr
    # An empty name would break every hook login, so upgrades refuse it too.
    result, _ = render(*name(""), "--is-upgrade")
    assert result.returncode != 0 and "keycloakAdminUsername is required" in result.stderr, result.stderr


def check_upgrade_keeps_legacy_name():
    # Existing installs keep their master name until the runbook migration is done.
    for value in ("realmadmin", "admin"):
        result, docs = render(*name(value), "--is-upgrade")
        assert result.returncode == 0, (value, result.stderr)
        master = base64.b64decode(docs[("Secret", "keycloak-secret-env")]["data"]["KEYCLOAK_ADMIN"]).decode()
        assert master == value, master


def reconcile_env(docs, name="keycloak-reconcile-service-identities"):
    container = docs[("Job", name)]["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value") for e in container["env"]}


def check_reconcile_flag(docs):
    env = reconcile_env(docs)
    assert env["DISABLE_REALM_ADMIN"] == "false" and env["REALM_ADMIN_USERNAME"] == "realmadmin", env
    flag = ("--set", "global.keycloak.bootstrapUsers.realmAdmin.disableExisting=true")
    result, on = render(*flag, "--is-upgrade")
    assert result.returncode == 0, result.stderr
    assert reconcile_env(on)["DISABLE_REALM_ADMIN"] == "true"
    # The bounded preparation Job never disables anyone.
    result, prep = render(*flag, "--set", "global.keycloak.backendServiceClients.prepareOnly=true")
    assert result.returncode == 0, result.stderr
    assert reconcile_env(prep, "keycloak-prepare-backend-clients")["DISABLE_REALM_ADMIN"] == "false"


def main():
    result, docs = render()
    assert result.returncode == 0, result.stderr
    check_fresh_import_disables_realm_admin(docs)
    check_no_automation_uses_realm_admin(docs)
    check_installer_chooses_master_name(docs)
    check_install_rejects_missing_or_well_known_name()
    check_upgrade_keeps_legacy_name()
    check_reconcile_flag(docs)
    print("PASS: break-glass separation")


if __name__ == "__main__":
    main()
