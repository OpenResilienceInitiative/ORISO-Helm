import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def render_chart():
    result = subprocess.run(
        [
            "helm",
            "template",
            "test",
            str(ROOT),
            "-f",
            str(ROOT / "values.yaml.default"),
            "-f",
            str(ROOT / "secrets.yaml.default"),
            "--set",
            "userService.smtpHost=",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def resource(docs, kind, name):
    for doc in docs:
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name:
            return doc
    raise AssertionError(f"{kind}/{name} was not rendered")


def assert_install_only_job(docs, name):
    job = resource(docs, "Job", name)
    annotations = job["metadata"].get("annotations", {})
    hooks = {hook.strip() for hook in annotations.get("helm.sh/hook", "").split(",")}
    assert hooks == {"post-install"}
    assert (
        annotations.get("helm.sh/hook-delete-policy")
        == "before-hook-creation,hook-succeeded"
    )
    return job


def container_script(job):
    command = job["spec"]["template"]["spec"]["containers"][0]["command"]
    return command[-1]


def test_seed_jobs_are_install_only_and_wait_for_liquibase():
    docs = render_chart()

    tenant_job = assert_install_only_job(docs, "tenant-bootstrap")
    topic_job = assert_install_only_job(docs, "topic-bootstrap")
    assert_install_only_job(docs, "keycloak-bootstrap-users")
    assert_install_only_job(docs, "matrixrtc-bootstrap-token")
    assert_install_only_job(docs, "create-mongo-users")

    for script in (container_script(tenant_job), container_script(topic_job)):
        assert "DATABASECHANGELOGLOCK" in script
        assert "LOCKED = 0" in script

    tenant_sql = (ROOT / "files" / "tenant-bootstrap.sql").read_text()
    topic_sql = (ROOT / "files" / "topic-bootstrap.sql").read_text()

    for sql in (tenant_sql, topic_sql):
        assert "INSERT IGNORE" in sql
        assert "DELETE FROM" not in sql.upper()
        assert "TRUNCATE" not in sql.upper()
        assert "SETVAL(`sequence_" in sql
