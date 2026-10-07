"""Rendered installation contract: real manifests, no production credentials."""
import base64
import json
import subprocess
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE = ["helm", "template", "task-identities", str(ROOT)]
for file in ["values.yaml.default", "tests/fixtures/values-render-domain.yaml", "secrets.yaml.default", "tests/fixtures/render-required-secrets.yaml"]:
    BASE += ["-f", str(ROOT / file)]

def render(*args):
    result = subprocess.run(BASE + list(args), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr[-1600:]
    return [item for item in yaml.safe_load_all(result.stdout) if item]

def resource(docs, kind, name):
    return next(x for x in docs if x.get("kind") == kind and x["metadata"]["name"] == name)

def test_fresh_import_uses_scoped_task_clients():
    docs = render()
    realm = json.loads(base64.b64decode(resource(docs, "Secret", "keycloak-realm-import")["data"]["realm.json"]))
    client = next((c for c in realm["clients"] if c["clientId"] == "backend-config-wizard"), None)
    assert client is not None, "Fresh import lacks Config Wizard service-account client"
    assert client["serviceAccountsEnabled"] is True
    assert client["directAccessGrantsEnabled"] is False
    assert client["standardFlowEnabled"] is False
    assert client["fullScopeAllowed"] is False
    assert client["defaultClientScopes"] == ["basic"]
    assert client["optionalClientScopes"] == []
    assert any(m["protocolMapper"] == "oidc-usermodel-realm-role-mapper" for m in client["protocolMappers"])
    bindings = resource(docs, "ConfigMap", "oriso-task-identity-bindings")["data"]
    user = next(u for u in realm["users"] if u.get("serviceAccountClientId") == "backend-config-wizard")
    assert user["id"] == bindings["IDENTITY_CONFIG_WIZARD_SERVICE_SUBJECT"]
    assert user["realmRoles"] == ["config-wizard"]
    assert not user.get("clientRoles")

def test_new_install_does_not_reenable_legacy_actors():
    docs = render()
    realm = json.loads(base64.b64decode(resource(docs, "Secret", "keycloak-realm-import")["data"]["realm.json"]))
    for name in ["technical", "svc-keycloak-admin"]:
        user = next(u for u in realm["users"] if u["username"] == name)
        assert user["enabled"] is False
        assert "otp-config-admin" not in user.get("realmRoles", [])
        assert not user.get("clientRoles")
    job = resource(docs, "Job", "keycloak-bootstrap-users")
    script = job["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    assert 'ensure_user "$TECHNICAL_USERNAME"' not in script
    for name in ["userservice-secret", "consultingtypeservice-secret", "agencyservice-secret"]:
        data = resource(docs, "Secret", name)["data"]
        assert not set(data) & {"IDENTITY_TECHNICAL_USER_PASSWORD", "KEYCLOAK_CONFIG_ADMIN_PASSWORD"}

def test_client_credentials_are_not_exposed_through_configmaps():
    docs = render()
    for doc in docs:
        if doc["kind"] == "ConfigMap":
            assert "render-only-config_wizard-credential" not in json.dumps(doc), doc["metadata"]["name"]

def test_changed_credentials_and_proofs_roll_only_the_consuming_workloads():
    before = render()
    after = render("--set-string", "global.taskIdentitySecrets.OTP=rotated-render-only-otp-client-secret")
    def annotations(docs, name):
        return resource(docs, "Deployment", name)["spec"]["template"]["metadata"].get("annotations", {})
    assert annotations(before, "userservice") != annotations(after, "userservice")
    assert annotations(before, "tenantservice") == annotations(after, "tenantservice")
    after = render("--set-string", "global.commandOriginKeys.wizardPolicy=cm90YXRlZC13aXphcmQtcG9saWN5LWNvbnRleHQta2V5")
    assert annotations(before, "userservice") != annotations(after, "userservice")
    assert annotations(before, "tenantservice") != annotations(after, "tenantservice")
    assert annotations(before, "agencyservice") == annotations(after, "agencyservice")

def test_missing_or_reused_task_credential_fails_without_rendering_secrets():
    for args in [("--set-string", "global.taskIdentitySecrets.CONFIG_WIZARD="),
                 ("--set-string", "global.taskIdentitySecrets.CONFIG_WIZARD=duplicate", "--set-string", "global.taskIdentitySecrets.SMTP_SYNC=duplicate")]:
        result = subprocess.run(BASE + list(args), text=True, capture_output=True)
        assert result.returncode != 0
        assert "requires a distinct persistent secret" in result.stderr
        assert "render-only-config_wizard-credential" not in result.stderr
        assert not result.stdout.strip()

def test_upgrade_prepares_task_clients_before_receiver_pods_start():
    docs=render("--is-upgrade")
    credentials=resource(docs,"Secret","oriso-task-identity-credentials")
    script=resource(docs,"ConfigMap","keycloak-reconcile-task-identities-script")
    job=resource(docs,"Job","keycloak-reconcile-service-identities")
    for item in [credentials,script,job]:
        hooks=item["metadata"].get("annotations",{}).get("helm.sh/hook","").split(",")
        assert "pre-upgrade" in hooks,item["metadata"]["name"]+" must precede receiver startup"
    assert "post-install" in job["metadata"]["annotations"]["helm.sh/hook"]
    assert "pre-install" not in job["metadata"]["annotations"]["helm.sh/hook"]


def test_runtime_does_not_receive_deployment_admin_credentials():
    docs = render()
    for name in ["userservice", "keycloak-reconcile-smtp"]:
        env = resource(docs, "Deployment", name)["spec"]["template"]["spec"]["containers"][0]["env"]
        names = {v["name"] for v in env}
        assert not names & {"KEYCLOAK_ADMIN_USERNAME", "KEYCLOAK_ADMIN_PASSWORD", "KEYCLOAK_CONFIG_ADMIN_PASSWORD", "IDENTITY_TECHNICAL_USER_PASSWORD"}
    smtp = resource(docs, "Deployment", "keycloak-reconcile-smtp")
    smtp_env = {v["name"] for v in smtp["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "KEYCLOAK_SMTP_SYNC_CLIENT_SECRET" in smtp_env
    user_env = {v["name"] for v in resource(docs,"Deployment","userservice")["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "IDENTITY_CONSULTANT_IMPORT_SERVICE_SUBJECT" in user_env
    assert "KEYCLOAK_CONSULTANT_IMPORT_CLIENT_SECRET" not in user_env
    for name in ["tenantservice", "agencyservice", "consultingtypeservice"]:
        env = resource(docs, "Deployment", name)["spec"]["template"]["spec"]["containers"][0]["env"]
        assert any(v["name"] == "TASK_IDENTITY_REQUIRED_TASKS" and v.get("value") for v in env)

if __name__ == "__main__":
    test_fresh_import_uses_scoped_task_clients()
    test_new_install_does_not_reenable_legacy_actors()
    test_client_credentials_are_not_exposed_through_configmaps()
    test_changed_credentials_and_proofs_roll_only_the_consuming_workloads()
    test_missing_or_reused_task_credential_fails_without_rendering_secrets()
    test_upgrade_prepares_task_clients_before_receiver_pods_start()
    test_runtime_does_not_receive_deployment_admin_credentials()
    print("PASS: scoped task clients, distinct credentials and runtime admin exclusion")
