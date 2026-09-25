"""Render contract for the Keycloak service identities (ORISO-Helm#367, stage 1: additive)."""

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


def test_reconcile_job_reads_the_service_admin_from_the_userservice_secret():
    docs = render()
    env = env_of(resource(docs, "Job", JOB))
    for name, key in (
        ("SERVICE_ADMIN_USERNAME", "KEYCLOAK_SERVICE_ADMIN_USERNAME"),
        ("SERVICE_ADMIN_PASSWORD", "KEYCLOAK_SERVICE_ADMIN_PASSWORD"),
    ):
        assert "value" not in env[name], "credentials must be references, not literals"
        assert env[name]["valueFrom"]["secretKeyRef"] == {"name": "userservice-secret", "key": key}
    data = secret_data(docs, "userservice-secret")
    assert data["KEYCLOAK_SERVICE_ADMIN_USERNAME"] == "svc-keycloak-admin"


def test_without_service_admin_values_the_job_still_renders_and_skips_the_identity():
    docs = render(
        "--set", "global.secrets.keycloakServiceAdminUsername=",
        "--set", "global.secrets.keycloakServiceAdminPassword=",
    )
    env = env_of(resource(docs, "Job", JOB))
    assert "SERVICE_ADMIN_USERNAME" not in env
    assert "KEYCLOAK_SERVICE_ADMIN_USERNAME" not in secret_data(docs, "userservice-secret")


def test_a_half_configured_service_admin_fails_the_render():
    result = render("--set", "global.secrets.keycloakServiceAdminPassword=", check=False)
    assert result.returncode != 0
    assert "keycloakServiceAdminPassword" in result.stderr


def test_stage_one_leaves_the_runtime_admin_credentials_untouched():
    # Additive stage: services keep calling with the existing admin identity until
    # the removal stage switches them over.
    docs = render()
    for secret in ("userservice-secret", "agencyservice-secret", "consultingtypeservice-secret"):
        assert secret_data(docs, secret)["KEYCLOAK_CONFIG_ADMIN_USERNAME"] == "realmadmin"
    userservice = env_of(resource(docs, "Deployment", "userservice"))
    assert "KEYCLOAK_SERVICE_ADMIN_USERNAME" not in userservice
    assert "KEYCLOAK_SERVICE_ADMIN_PASSWORD" not in userservice


def test_helm_realm_copy_seeds_the_service_admin_with_exact_roles():
    realm = json.loads((ROOT / "charts/keycloak/keycloak-resources/realm.json").read_text())
    assert "otp-config-admin" in {role["name"] for role in realm["roles"]["realm"]}
    admin = next(user for user in realm["users"] if user["username"] == "svc-keycloak-admin")
    assert sorted(admin["realmRoles"]) == ["default-roles-online-beratung", "otp-config-admin"]
    assert admin["clientRoles"] == {
        "realm-management": ["manage-users", "view-users", "query-users", "view-realm"]
    }
    assert not admin.get("credentials")


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
    print("PASS: Keycloak service identities render contract")
