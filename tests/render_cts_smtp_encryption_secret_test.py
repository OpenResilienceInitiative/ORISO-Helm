"""The saved platform SMTP password needs one persistent CTS encryption key."""

import base64
import pathlib
import shutil
import subprocess
import tempfile

import yaml


CHART = pathlib.Path(__file__).resolve().parents[1]
KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()


def render(values):
    with tempfile.TemporaryDirectory() as temp:
        chart = pathlib.Path(temp)
        (chart / "templates").mkdir()
        shutil.copy(CHART / "templates/consultingtypeservice/consultingtypeservice-secret.yaml", chart / "templates/secret.yaml")
        (chart / "Chart.yaml").write_text("apiVersion: v2\nname: cts-smtp-key-test\nversion: 0.0.0\n")
        (chart / "values.yaml").write_text(yaml.safe_dump(values))
        return subprocess.run(["helm", "template", "test", str(chart)], capture_output=True, text=True)


def base_values():
    return {"consultingTypeService": {}, "global": {"secrets": {
        "consultingTypeMongoUser": "mongo", "consultingTypeMongoPass": "mongo-pass",
        "liquibaseUser": "liquibase", "liquibasePassword": "liquibase-pass",
        "consultingTypeServiceDbUsername": "cts", "consultingTypeServiceDbPassword": "cts-pass",
    }}}


def test_missing_or_invalid_key_fails_at_render():
    for value in (None, "", "changeme", "YWJjZA=="):
        values = base_values()
        if value is not None:
            values["consultingTypeService"]["smtpPasswordEncryptionSecret"] = value
        result = render(values)
        assert result.returncode != 0
        assert "consultingTypeService.smtpPasswordEncryptionSecret" in result.stderr


def test_key_is_in_secret_and_deployment_imports_it():
    values = base_values()
    values["consultingTypeService"]["smtpPasswordEncryptionSecret"] = KEY
    result = render(values)
    assert result.returncode == 0, result.stderr
    document = yaml.safe_load(result.stdout)
    encoded = document["data"]["SETTINGS_SMTP_PASSWORD_ENCRYPTION_SECRET"]
    assert base64.b64decode(encoded).decode() == KEY

    full = subprocess.run(
        ["helm", "template", "cts-smtp-key", str(CHART),
         "-f", str(CHART / "values.yaml.default"),
         "-f", str(CHART / "secrets.yaml.default"),
         "-f", str(CHART / "tests/fixtures/values-render-domain.yaml"),
         "-f", str(CHART / "tests/fixtures/render-required-secrets.yaml")],
        capture_output=True, text=True,
    )
    assert full.returncode == 0, full.stderr
    documents = [d for d in yaml.safe_load_all(full.stdout) if isinstance(d, dict)]
    secret = next(d for d in documents if d.get("kind") == "Secret" and d["metadata"]["name"] == "consultingtypeservice-secret")
    deployment = next(d for d in documents if d.get("kind") == "Deployment" and d["metadata"]["name"] == "consultingtypeservice")
    assert base64.b64decode(secret["data"]["SETTINGS_SMTP_PASSWORD_ENCRYPTION_SECRET"]).decode() == KEY
    env = next(e for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"] if e["name"] == "SETTINGS_SMTP_PASSWORD_ENCRYPTION_SECRET")
    assert env["valueFrom"]["secretKeyRef"] == {
        "key": "SETTINGS_SMTP_PASSWORD_ENCRYPTION_SECRET",
        "name": "consultingtypeservice-secret",
    }
