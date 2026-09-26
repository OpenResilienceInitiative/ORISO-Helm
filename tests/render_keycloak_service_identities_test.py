"""Render contract for the Keycloak service identities (ORISO-Helm#367, stage 2: removals)."""

import base64
import json
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
JOB = "keycloak-reconcile-service-identities"


def render(*overrides, check=True):
    result = subprocess.run(
        [
            "helm", "template", "identities", str(ROOT),
            "-f", str(ROOT / "values.yaml.default"),
            "-f", str(ROOT / "tests" / "fixtures" / "values-render-domain.yaml"),
            "-f", str(ROOT / "secrets.yaml.default"),
            "-f", str(ROOT / "tests" / "fixtures" / "render-required-secrets.yaml"),
            "--set-string", "userService.smtpUser=smtp-canary-user",
            "--set-string", "userService.smtpPassword=smtp-canary-password",
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
    assert hooks == {"post-install", "post-upgrade"}
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
    job = resource(render(), "Job", JOB)
    script = job["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    source = (ROOT / "files" / "keycloak-reconcile-service-identities.sh.in").read_text()
    assert "reconcile_roles" in script
    assert source.split("set -eu", 1)[1].strip() in script


RUNTIME_SECRETS = ("userservice-secret", "agencyservice-secret", "consultingtypeservice-secret")


def test_reconcile_job_reads_both_identities_from_the_userservice_secret():
    env = env_of(resource(render(), "Job", JOB))
    for name, key in (
        ("SERVICE_ADMIN_USERNAME", "KEYCLOAK_CONFIG_ADMIN_USERNAME"),
        ("SERVICE_ADMIN_PASSWORD", "KEYCLOAK_CONFIG_ADMIN_PASSWORD"),
        ("TECHNICAL_USERNAME", "IDENTITY_TECHNICAL_USER_USERNAME"),
    ):
        assert "value" not in env[name], "credentials must be references, not literals"
        assert env[name]["valueFrom"]["secretKeyRef"] == {"name": "userservice-secret", "key": key}


def test_the_userservice_calls_keycloak_admin_as_the_service_admin():
    docs = render()
    data = secret_data(docs, "userservice-secret")
    assert data["KEYCLOAK_CONFIG_ADMIN_USERNAME"] == "svc-keycloak-admin"
    env = env_of(resource(docs, "Deployment", "userservice"))
    for name in ("KEYCLOAK_CONFIG_ADMIN_USERNAME", "KEYCLOAK_CONFIG_ADMIN_PASSWORD"):
        assert env[name]["valueFrom"]["secretKeyRef"] == {"name": "userservice-secret", "key": name}


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


def test_the_duplicate_technical_secret_pair_is_gone():
    docs = render()
    data = secret_data(docs, "userservice-secret")
    assert "KEYCLOAKSERVICE_TECHNICAL_USERNAME" not in data
    assert "KEYCLOAKSERVICE_TECHNICAL_PASSWORD" not in data
    assert "KEYCLOAK_SERVICE_ADMIN_USERNAME" not in data
    env = env_of(resource(docs, "Deployment", "userservice"))
    assert not [name for name in env if name.startswith("KEYCLOAKSERVICE_TECHNICAL_")]
    bootstrap = env_of(resource(docs, "Job", "keycloak-bootstrap-users"))
    assert bootstrap["TECHNICAL_USERNAME"]["valueFrom"]["secretKeyRef"]["key"] == "IDENTITY_TECHNICAL_USER_USERNAME"
    assert bootstrap["TECHNICAL_PASSWORD"]["valueFrom"]["secretKeyRef"]["key"] == "IDENTITY_TECHNICAL_USER_PASSWORD"


def test_the_bootstrap_job_seeds_technical_without_admin_roles():
    job = resource(render(), "Job", "keycloak-bootstrap-users")
    script = job["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    technical_part = script.split('ensure_user "$TECHNICAL_USERNAME"', 1)[1]
    assert '--rolename "technical"' in technical_part
    for role in ("TECHNICAL_DEFAULT", "tenant-admin", "manage-users", "view-users", "query-users"):
        assert f'"{role}"' not in technical_part, role
    assert "TECHNICAL_DEFAULT" not in script


def test_missing_values_fail_the_render():
    for value in (
        "global.secrets.keycloakServiceAdminUsername",
        "global.secrets.keycloakServiceAdminPassword",
        "userService.identityTechnicalUserUsername",
        "userService.identityTechnicalUserPassword",
    ):
        result = render("--set", f"{value}=", check=False)
        assert result.returncode != 0, value
        assert value.rsplit(".", 1)[1] in result.stderr, (value, result.stderr[-300:])


TECHNICAL_ID = "8294c392-e1e0-405b-ac2f-ba3043cbad3e"
FIXTURE_SUBJECT = "00000000-0000-4000-8000-000000000000"  # tests/fixtures/render-required-secrets.yaml


def test_tenantservice_receives_the_technical_subject():
    docs = render()
    data = next(doc for doc in docs if doc.get("kind") == "ConfigMap"
                and doc["metadata"]["name"] == "tenantservice-configmap-env")["data"]
    assert data["TECHNICAL_SERVICE_SUBJECT"] == FIXTURE_SUBJECT
    env = env_of(resource(docs, "Deployment", "tenantservice"))
    assert env["TECHNICAL_SERVICE_SUBJECT"]["valueFrom"]["configMapKeyRef"] == {
        "key": "TECHNICAL_SERVICE_SUBJECT", "name": "tenantservice-configmap-env"}
    # The reconcile hook checks the live realm against the same value.
    job_env = env_of(resource(docs, "Job", JOB))
    assert job_env["TECHNICAL_SERVICE_SUBJECT"]["valueFrom"]["configMapKeyRef"] == {
        "key": "TECHNICAL_SERVICE_SUBJECT", "name": "tenantservice-configmap-env"}


def test_the_technical_subject_must_be_a_keycloak_user_id():
    for bad in ("", "changeme", "technical"):
        result = render("--set-string", f"global.keycloak.serviceTechUserId={bad}", check=False)
        assert result.returncode != 0, bad
        assert "serviceTechUserId" in result.stderr, bad


def test_the_technical_subject_has_no_default():
    # An existing realm keeps its own id; a shipped default would be wrong there.
    values = yaml.safe_load((ROOT / "values.yaml.default").read_text())
    assert not values["global"]["keycloak"].get("serviceTechUserId")
    secrets = yaml.safe_load((ROOT / "secrets.yaml.default").read_text())
    assert not secrets["global"].get("keycloak", {}).get("serviceTechUserId")
    result = subprocess.run(
        ["helm", "template", "no-subject", str(ROOT),
         "-f", str(ROOT / "values.yaml.default"),
         "-f", str(ROOT / "tests" / "fixtures" / "values-render-domain.yaml"),
         "-f", str(ROOT / "secrets.yaml.default"),
         "--set-string", "global.secrets.keycloakServiceAdminPassword=a-real-one",
         "--set-string", "userService.smtpUser=smtp-canary-user",
         "--set-string", "userService.smtpPassword=smtp-canary-password"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "serviceTechUserId" in result.stderr


def test_placeholder_admin_passwords_fail_the_render():
    for bad in ("", "changeme"):
        result = render("--set-string", f"global.secrets.keycloakServiceAdminPassword={bad}", check=False)
        assert result.returncode != 0, bad
        assert "keycloakServiceAdminPassword" in result.stderr, bad


def test_the_reconcile_job_renders_even_without_the_bootstrap_job():
    docs = render("--set", "global.keycloak.bootstrapUsers.enabled=false")
    assert not [d for d in docs if d.get("kind") == "Job" and d["metadata"]["name"] == "keycloak-bootstrap-users"]
    resource(docs, "Job", JOB)


def test_the_reconcile_job_knows_the_bootstrap_admin_username():
    env = env_of(resource(render(), "Job", JOB))
    assert env["BOOTSTRAP_ADMIN_USERNAME"]["valueFrom"]["secretKeyRef"] == {
        "name": "keycloak-secret-env", "key": "KEYCLOAK_ADMIN"}


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


def test_helm_realm_copy_seeds_the_service_admin_with_exact_roles():
    realm = json.loads((ROOT / "charts/keycloak/keycloak-resources/realm.json").read_text())
    assert "otp-config-admin" in {role["name"] for role in realm["roles"]["realm"]}
    admin = next(user for user in realm["users"] if user["username"] == "svc-keycloak-admin")
    assert sorted(admin["realmRoles"]) == ["default-roles-online-beratung", "otp-config-admin"]
    assert admin["clientRoles"] == {
        "realm-management": ["manage-users", "view-users", "query-users", "view-realm"]
    }
    assert not admin.get("credentials")
    technical = next(user for user in realm["users"] if user["username"] == "technical")
    assert sorted(technical["realmRoles"]) == ["default-roles-online-beratung", "technical"]
    assert not technical.get("clientRoles")
    assert "TECHNICAL_DEFAULT" not in {role["name"] for role in realm["roles"]["realm"]}


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
    print("PASS: Keycloak service identities render contract")
