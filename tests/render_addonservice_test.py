#!/usr/bin/env python3
"""Render guard for the optional AddonService (ADR-034).

  1. Off by default: an untouched chart renders none of its resources.
  2. Enabled, it renders the service, its database Job, discovery RBAC limited
     to Services in the add-on namespace, and the /service/addons route.
  3. The database Job name follows its rendered pod template, so any template
     change (pull policy, image, username, ...) creates a new Job instead of
     an upgrade rejected because a Job's template is immutable.
  4. It refuses to render without an image, a database password or a PKCS#8
     signing key, so a half-configured install fails at deploy time.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = [
    "templates/_helpers.tpl",
    "templates/_account-inactivity.tpl",
    "templates/_addonservice.tpl",
    "templates/addonservice/addonservice-configmap-env.yaml",
    "templates/addonservice/addonservice-db-job.yaml",
    "templates/addonservice/addonservice-deployment.yaml",
    "templates/addonservice/addonservice-pdb.yaml",
    "templates/addonservice/addonservice-rbac.yaml",
    "templates/addonservice/addonservice-secret.yaml",
    "templates/addonservice/addonservice-service.yaml",
    "templates/nginx/addonservice-rewrite-ingress.yaml",
]
RESOURCES = [
    ("ConfigMap", "addonservice-configmap-env"),
    ("Secret", "addonservice-secret"),
    ("Deployment", "addonservice"),
    ("Service", "addonservice"),
    ("PodDisruptionBudget", "addonservice-pdb"),
    ("ServiceAccount", "addonservice"),
    ("Role", "addonservice-discovery-addon-test"),
    ("RoleBinding", "addonservice-discovery-addon-test"),
    ("Ingress", "addonservice-rewrite-ingress"),
]
RENDER_ONLY_KEY = """-----BEGIN PRIVATE KEY-----
render-only-not-a-real-key
-----END PRIVATE KEY-----
"""


def build_minimal_chart(destination: str) -> None:
    os.makedirs(os.path.join(destination, "templates"))
    with open(os.path.join(destination, "Chart.yaml"), "w", encoding="utf-8") as chart:
        chart.write("apiVersion: v2\nname: addonservice\nversion: 0.0.0\nappVersion: 0.0.0\n")
    shutil.copyfile(
        os.path.join(CHART_DIR, "values.yaml.default"),
        os.path.join(destination, "values.yaml"),
    )
    for relative_path in TEMPLATES:
        target = os.path.join(destination, relative_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(os.path.join(CHART_DIR, relative_path), target)


def helm_template(chart: str, values: dict | None, scratch: str) -> subprocess.CompletedProcess:
    if shutil.which("helm") is None:
        raise AssertionError("helm is required to run this guard")
    command = [
        "helm", "template", "addonservice", chart,
        "--namespace", "addon-test",
        "--set-string", "global.domainName=render.oriso.internal",
    ]
    if values is not None:
        path = os.path.join(scratch, "values.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(values, handle)
        command.extend(["-f", path])
    return subprocess.run(command, capture_output=True, text=True, check=False)


def render(chart: str, scratch: str, values: dict | None = None) -> list[dict]:
    result = helm_template(chart, values, scratch)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def render_error(chart: str, scratch: str, values: dict) -> str:
    result = helm_template(chart, values, scratch)
    if not result.returncode:
        raise AssertionError("expected the render to fail, but it succeeded")
    return result.stderr


def find(documents: list[dict], kind: str, name: str) -> dict | None:
    for document in documents:
        if document.get("kind") == kind and document.get("metadata", {}).get("name") == name:
            return document
    return None


def get(documents: list[dict], kind: str, name: str) -> dict:
    document = find(documents, kind, name)
    assert document is not None, "missing %s/%s" % (kind, name)
    return document


def enabled_values(**overrides) -> dict:
    addon_service = {
        "enabled": True,
        "image": {"repository": "registry.render.oriso.internal/addonservice", "tag": "0.1.0"},
        "tokenPrivateKeyPem": RENDER_ONLY_KEY,
    }
    addon_service.update(overrides)
    return {
        "addonService": addon_service,
        "global": {"secrets": {"addonServiceDbPassword": "render-only-db-password"}},
    }


def assert_off_by_default(chart: str, scratch: str) -> None:
    documents = render(chart, scratch)
    assert documents == [], "AddonService resources render without opt-in: %s" % [
        (document["kind"], document["metadata"]["name"]) for document in documents
    ]


def assert_missing_section_is_off(chart: str, scratch: str) -> None:
    values_path = os.path.join(chart, "values.yaml")
    with open(values_path, encoding="utf-8") as handle:
        values = yaml.safe_load(handle)
    values.pop("addonService")
    with open(values_path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(values, handle)
    try:
        assert render(chart, scratch) == [], "a values file without addonService must render nothing"
    finally:
        shutil.copyfile(os.path.join(CHART_DIR, "values.yaml.default"), values_path)


def assert_enabled(chart: str, scratch: str) -> None:
    documents = render(chart, scratch, enabled_values())
    for kind, name in RESOURCES:
        get(documents, kind, name)

    jobs = [document for document in documents if document["kind"] == "Job"]
    assert len(jobs) == 1 and jobs[0]["metadata"]["name"].startswith("addonservice-db-init-")
    assert "helm.sh/hook" not in jobs[0]["metadata"].get("annotations", {}), (
        "the database Job must not wait for a healthy release; the service needs it to start"
    )

    config = get(documents, "ConfigMap", "addonservice-configmap-env")["data"]
    assert config["SPRING_DATASOURCE_URL"] == "jdbc:mariadb://mariadb.addon-test:3306/addonservice"
    assert config["ADDONS_DISCOVERY_KUBERNETES"] == "true"
    assert config["ADDONS_DISCOVERY_NAMESPACE"] == "addons"
    assert config["ADDONS_USERSERVICE_BASEURL"] == "http://userservice.addon-test:8080"
    assert config["ADDONS_TENANTSERVICE_BASEURL"] == "http://tenantservice.addon-test:8081"
    assert config["ADDONS_LIFECYCLE_ENABLED"] == "true"
    assert config["SPRING_RABBITMQ_HOST"] == "rabbitmq"

    pod = get(documents, "Deployment", "addonservice")["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "addonservice"
    container = pod["containers"][0]
    assert container["ports"][0]["containerPort"] == 8090
    assert container["livenessProbe"]["httpGet"]["path"] == "/actuator/health/liveness"
    assert container["readinessProbe"]["httpGet"]["path"] == "/actuator/health/readiness"
    environment = {entry["name"]: entry["valueFrom"] for entry in container["env"]}
    assert environment["ADDONS_TOKEN_PRIVATEKEYPEM"]["secretKeyRef"]["name"] == "addonservice-secret"
    assert environment["SPRING_DATASOURCE_PASSWORD"]["secretKeyRef"]["name"] == "addonservice-secret"
    assert environment["SPRING_RABBITMQ_PASSWORD"]["secretKeyRef"]["name"] == "addonservice-secret"
    assert environment["ADDONS_TECHNICALCLIENT_CLIENTSECRET"]["secretKeyRef"] == {
        "key": "KEYCLOAK_BACKEND_TECHNICAL_CLIENT_SECRET",
        "name": "keycloak-backend-client-secrets",
    }
    assert "ADDONS_TECHNICALCLIENT_TOKENURL" in environment

    role = get(documents, "Role", "addonservice-discovery-addon-test")
    assert role["metadata"]["namespace"] == "addons"
    assert role["rules"] == [{"apiGroups": [""], "resources": ["services"], "verbs": ["get", "list"]}]
    binding = get(documents, "RoleBinding", "addonservice-discovery-addon-test")
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "addonservice", "namespace": "addon-test"}
    ]
    assert find(documents, "Namespace", "addons") is None

    ingress = get(documents, "Ingress", "addonservice-rewrite-ingress")
    path = ingress["spec"]["rules"][0]["http"]["paths"][0]
    assert path["path"] == "/service/(addons)(/|$)(.*)"
    assert path["backend"]["service"] == {"name": "addonservice", "port": {"number": 8090}}
    assert ingress["metadata"]["annotations"]["nginx.ingress.kubernetes.io/rewrite-target"] == "/$1$2$3"


def assert_static_discovery_only(chart: str, scratch: str) -> None:
    documents = render(
        chart,
        scratch,
        enabled_values(discovery={"kubernetes": False, "staticAddons": ["http://a:8080", "http://b:8080"]}),
    )
    config = get(documents, "ConfigMap", "addonservice-configmap-env")["data"]
    assert config["ADDONS_DISCOVERY_KUBERNETES"] == "false"
    assert config["ADDONS_DISCOVERY_STATICADDONS"] == "http://a:8080,http://b:8080"
    assert not [document for document in documents if document["kind"] in ("Role", "RoleBinding")], (
        "no RBAC is needed without Kubernetes discovery"
    )


def assert_lifecycle_can_be_turned_off(chart: str, scratch: str) -> None:
    documents = render(chart, scratch, enabled_values(lifecycle={"enabled": False}))
    config = get(documents, "ConfigMap", "addonservice-configmap-env")["data"]
    assert config["ADDONS_LIFECYCLE_ENABLED"] == "false"


def db_job(chart: str, scratch: str, values: dict) -> dict:
    jobs = [document for document in render(chart, scratch, values) if document["kind"] == "Job"]
    assert len(jobs) == 1, "expected one database Job, got %d" % len(jobs)
    return jobs[0]


def assert_db_job_name_follows_pod_template(chart: str, scratch: str) -> None:
    def with_global(**global_values) -> dict:
        values = enabled_values()
        values["global"].update(global_values)
        return values

    def with_mariadb_tag(tag: str) -> dict:
        values = enabled_values()
        values["online-counseling-mariadb"] = {"imageVersion": tag}
        return values

    def with_db_username(username: str) -> dict:
        values = enabled_values()
        values["global"]["secrets"]["addonServiceDbUsername"] = username
        return values

    baseline = db_job(chart, scratch, with_global(imagePullPolicy="Always"))
    name = baseline["metadata"]["name"]
    assert db_job(chart, scratch, with_global(imagePullPolicy="Always"))["metadata"]["name"] == name, (
        "identical values must keep the database Job name"
    )

    variants = {
        "imagePullPolicy": with_global(imagePullPolicy="IfNotPresent"),
        "MariaDB image": with_mariadb_tag("render-only-other-tag"),
        "database username": with_db_username("render-only-other-user"),
    }
    for change, values in variants.items():
        job = db_job(chart, scratch, values)
        assert job["metadata"]["name"] != name, (
            "changing the %s changes the pod template but keeps the Job name %s" % (change, name)
        )
        assert job["spec"]["template"] != baseline["spec"]["template"], change

    pull_policy = db_job(chart, scratch, variants["imagePullPolicy"])["spec"]["template"]["spec"]
    assert pull_policy["containers"][0]["imagePullPolicy"] == "IfNotPresent"


def assert_refuses_incomplete(chart: str, scratch: str) -> None:
    error = render_error(chart, scratch, enabled_values(image={"repository": "", "tag": ""}))
    assert "addonService.image.repository" in error, error

    error = render_error(chart, scratch, enabled_values(tokenPrivateKeyPem=""))
    assert "addonService.tokenPrivateKeyPem is required" in error, error

    error = render_error(
        chart, scratch, enabled_values(tokenPrivateKeyPem="-----BEGIN RSA PRIVATE KEY-----\n")
    )
    assert "PKCS#8" in error, error

    values = enabled_values()
    values["global"]["secrets"]["addonServiceDbPassword"] = ""
    error = render_error(chart, scratch, values)
    assert "global.secrets.addonServiceDbPassword is required" in error, error


def main() -> None:
    temporary_directory = tempfile.mkdtemp(prefix="addonservice-render-")
    try:
        chart = os.path.join(temporary_directory, "chart")
        build_minimal_chart(chart)

        assert_off_by_default(chart, temporary_directory)
        assert_missing_section_is_off(chart, temporary_directory)
        assert_enabled(chart, temporary_directory)
        assert_static_discovery_only(chart, temporary_directory)
        assert_lifecycle_can_be_turned_off(chart, temporary_directory)
        assert_db_job_name_follows_pod_template(chart, temporary_directory)
        assert_refuses_incomplete(chart, temporary_directory)
    finally:
        shutil.rmtree(temporary_directory, ignore_errors=True)

    print("addonservice render guard: OK")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as error:
        print("addonservice render guard: FAIL\n%s" % error, file=sys.stderr)
        sys.exit(1)
