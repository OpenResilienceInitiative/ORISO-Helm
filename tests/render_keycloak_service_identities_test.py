"""Render contract for the Keycloak service identities (ORISO-Helm#367, stage 2: removals)."""

import base64
import json
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
JOB = "keycloak-reconcile-service-identities"
RUNTIME_SECRETS = ("userservice-secret", "agencyservice-secret", "consultingtypeservice-secret")
TECHNICAL_ID = "8294c392-e1e0-405b-ac2f-ba3043cbad3e"
FIXTURE_SUBJECT = "00000000-0000-4000-8000-000000000000"


def render(*overrides, check=True):
    result = subprocess.run(
        [
            "helm", "template", "identities", str(ROOT),
            "-f", str(ROOT / "values.yaml.default"),
            "-f", str(ROOT / "tests" / "fixtures" / "values-render-domain.yaml"),
            "-f", str(ROOT / "secrets.yaml.default"),
            "-f", str(ROOT / "tests" / "fixtures" / "render-required-secrets.yaml"),
            "--set", "userService.smtpHost=",
            *overrides,
        ],
        capture_output=True, text=True, check=False,
    )
    if check:
        assert result.returncode == 0, result.stderr
        return [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    return result


def resource(docs, kind, name):
    for doc in docs:
        if doc.get("kind") == kind and doc["metadata"]["name"] == name:
            return doc
    raise AssertionError(f"{kind}/{name} was not rendered")


def env_of(workload):
    container = workload["spec"]["template"]["spec"]["containers"][0]
    return {item["name"]: item for item in container.get("env", [])}


def secret_data(docs, name):
    data = resource(docs, "Secret", name)["data"]
    return {key: base64.b64decode(value).decode() for key, value in data.items()}


def test_reconcile_job_runs_on_install_and_upgrade():
    job = resource(render(), "Job", JOB)
    annotations = job["metadata"]["annotations"]
    hooks = {hook.strip() for hook in annotations["helm.sh/hook"].split(",")}
    assert hooks == {"post-install", "pre-upgrade"}
    # After keycloak-bootstrap-users (10), before keycloak-verify-2fa-contract (20).
    assert int(annotations["helm.sh/hook-weight"]) == 15
    assert job["spec"]["activeDeadlineSeconds"] > 0


def test_reconcile_job_uses_the_configured_admin_url():
    docs = render(
        "--set", "global.keycloak.verifyTwoFactorContract.adminUrl=http://kc-admin.{{ .Release.Namespace }}:9090/auth",
        "--namespace", "identities-ns",
    )
    env = env_of(resource(docs, "Job", JOB))
    assert env["KEYCLOAK_URL"]["value"] == "http://kc-admin.identities-ns:9090/auth"


def test_reconcile_job_without_an_admin_url_fails_the_render():
    result = render("--set", "global.keycloak.verifyTwoFactorContract.adminUrl=", check=False)
    assert result.returncode != 0
    assert "adminUrl" in result.stderr


def test_reconcile_job_embeds_the_tested_script():
    docs = render()
    job = resource(docs, "Job", JOB)
    assert job["spec"]["template"]["spec"]["containers"][0]["command"] == ["python3", "-B", "/scripts/keycloak-reconcile-task-identities.py"]
    source = (ROOT / "files/keycloak-reconcile-task-identities.py").read_text()
    config = resource(docs, "ConfigMap", "keycloak-reconcile-task-identities-script")
    assert config["data"]["keycloak-reconcile-task-identities.py"].strip() == source.strip()

def test_reconcile_job_reads_task_credentials_from_the_managed_secret():
    env = env_of(resource(render(), "Job", JOB))
    assert env["ORISO_TASK_IDENTITIES_JSON"]["valueFrom"]["secretKeyRef"] == {"name": "oriso-task-identity-credentials", "key": "ORISO_TASK_IDENTITIES_JSON"}
    assert "SERVICE_ADMIN_PASSWORD" not in env
    assert "TECHNICAL_USERNAME" not in env

def test_runtime_has_task_credentials_and_no_native_admin_credential():
    docs = render()
    data = secret_data(docs, "userservice-secret")
    assert "KEYCLOAK_CONFIG_ADMIN_USERNAME" not in data
    assert "KEYCLOAK_CONFIG_ADMIN_PASSWORD" not in data
    env = env_of(resource(docs, "Deployment", "userservice"))
    assert "KEYCLOAK_CONFIG_ADMIN_PASSWORD" not in env
    assert env["KEYCLOAK_ACCOUNT_PROVISIONING_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"] == {"name": "oriso-task-identity-credentials", "key": "KEYCLOAK_ACCOUNT_PROVISIONING_CLIENT_SECRET"}

def test_runtime_services_no_longer_receive_the_realmadmin_credentials():
    realmadmin_password = "realmadmin-canary-password"
    docs = render("--set-string", f"global.secrets.keycloakAdminPassword={realmadmin_password}")
    for secret in RUNTIME_SECRETS:
        data = secret_data(docs, secret)
        assert "realmadmin" not in data.values(), secret
        assert realmadmin_password not in data.values(), secret
    # AgencyService and ConsultingTypeService never used them.
    for secret in ("agencyservice-secret", "consultingtypeservice-secret"):
        assert "KEYCLOAK_CONFIG_ADMIN_USERNAME" not in secret_data(docs, secret)
    # Deployment-time only: the Keycloak pod and the bootstrap hooks keep them.
    assert secret_data(docs, "keycloak-secret-env")["KEYCLOAK_ADMIN_PASSWORD"] == realmadmin_password


def test_the_retired_runtime_password_sources_are_gone():
    docs = render()
    data = secret_data(docs, "userservice-secret")
    for name in ("KEYCLOAKSERVICE_TECHNICAL_USERNAME", "KEYCLOAKSERVICE_TECHNICAL_PASSWORD", "IDENTITY_TECHNICAL_USER_PASSWORD", "KEYCLOAK_CONFIG_ADMIN_PASSWORD"):
        assert name not in data
    env = env_of(resource(docs, "Job", "keycloak-bootstrap-users"))
    assert not any(name.startswith("TECHNICAL_") for name in env)

def test_the_bootstrap_job_never_reenables_retired_actors():
    job = resource(render(), "Job", "keycloak-bootstrap-users")
    script = job["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    assert 'ensure_user "$TECHNICAL_USERNAME"' not in script
    assert 'ensure_user "$REALM_ADMIN_USERNAME"' in script

def test_missing_task_credentials_fail_the_render():
    for key in ("CONFIG_WIZARD", "ACCOUNT_PROVISIONING", "ACCOUNT_MAINTENANCE", "OTP", "SMTP_SYNC"):
        result = render("--set", f"global.taskIdentitySecrets.{key}=", check=False)
        assert result.returncode != 0
        assert key in result.stderr

def test_legacy_subject_only_binds_explicit_compatibility():
    docs = render()
    data = resource(docs, "ConfigMap", "tenantservice-configmap-env")["data"]
    assert data["TECHNICAL_SERVICE_SUBJECT"] == FIXTURE_SUBJECT
    env = env_of(resource(docs, "Deployment", "tenantservice"))
    assert env["TECHNICAL_SERVICE_SUBJECT"]["valueFrom"]["configMapKeyRef"] == {"key": "TECHNICAL_SERVICE_SUBJECT", "name": "tenantservice-configmap-env"}
    assert "TECHNICAL_SERVICE_SUBJECT" not in env_of(resource(docs, "Job", JOB))

def test_nonempty_legacy_subject_must_be_a_keycloak_user_id():
    for bad in ("changeme", "technical"):
        result = render("--set-string", f"global.keycloak.serviceTechUserId={bad}", check=False)
        assert result.returncode != 0
        assert "serviceTechUserId" in result.stderr
    docs = render("--set-string", "global.keycloak.serviceTechUserId=")
    assert resource(docs, "ConfigMap", "tenantservice-configmap-env")["data"]["TECHNICAL_SERVICE_SUBJECT"] == ""

def test_legacy_compatibility_has_no_implicit_default_subject():
    values = yaml.safe_load((ROOT / "values.yaml.default").read_text())
    assert not values["global"]["keycloak"].get("serviceTechUserId")
    assert values["global"]["taskIdentities"]["retireLegacy"] is False

def test_retirement_requires_all_verified_legacy_subjects():
    result = render("--set", "global.taskIdentities.retireLegacy=true", check=False)
    assert result.returncode != 0
    assert "legacySubjects" in result.stderr

def test_the_reconcile_job_renders_even_without_the_bootstrap_job():
    docs = render("--set", "global.keycloak.bootstrapUsers.enabled=false")
    assert not [d for d in docs if d.get("kind") == "Job" and d["metadata"]["name"] == "keycloak-bootstrap-users"]
    resource(docs, "Job", JOB)


def test_only_the_reconcile_hook_has_installer_credentials():
    env = env_of(resource(render(), "Job", JOB))
    assert env["KEYCLOAK_ADMIN_USERNAME"]["valueFrom"]["secretKeyRef"] == {"name": "keycloak-secret-env", "key": "KEYCLOAK_ADMIN"}

def test_fresh_realms_seed_technical_with_the_default_subject():
    realm = json.loads((ROOT / "charts/keycloak/keycloak-resources/realm.json").read_text())
    technical = next(user for user in realm["users"] if user["username"] == "technical")
    assert technical["id"] == TECHNICAL_ID


def test_agencyservice_no_longer_receives_app_base_url():
    docs = render()
    assert "APP_BASE_URL" not in env_of(resource(docs, "Deployment", "agencyservice"))
    data = next(doc for doc in docs if doc.get("kind") == "ConfigMap"
                and doc["metadata"]["name"] == "agencyservice-configmap-env")["data"]
    assert "APP_BASE_URL" not in data


def test_fresh_realms_have_dedicated_otp_and_zero_retired_admin_grants():
    realm = json.loads((ROOT / "charts/keycloak/keycloak-resources/realm.json").read_text())
    assert "otp-config-admin" in {role["name"] for role in realm["roles"]["realm"]}
    actor = next(u for u in realm["users"] if u.get("serviceAccountClientId") == "backend-account-otp")
    assert actor["realmRoles"] == ["otp-config-admin"]
    assert not actor.get("clientRoles")
    for name in ("technical", "svc-keycloak-admin"):
        old = next(u for u in realm["users"] if u["username"] == name)
        assert old["enabled"] is False
        assert old["realmRoles"] == []
        assert not old.get("clientRoles")
    assert "TECHNICAL_DEFAULT" not in {role["name"] for role in realm["roles"]["realm"]}

if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
    print("PASS: Keycloak service identities render contract")
