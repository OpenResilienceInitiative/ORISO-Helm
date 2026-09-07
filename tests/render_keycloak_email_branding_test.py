#!/usr/bin/env python3
"""Render the real Keycloak deployment with a distinct mail origin."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as directory:
    chart = Path(directory)
    (chart / "templates").mkdir()
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: email-test\nversion: 0.1.0\n")
    shutil.copy(root / "charts/keycloak/templates/keycloak-deployment.yaml", chart / "templates/deployment.yaml")
    shutil.copy(root / "templates/_helpers.tpl", chart / "templates/_helpers.tpl")
    values = {"global": {"enableTls": True}, "imageName": "test", "imageVersion": "test",
              "emailBranding": {"appUrl": "https://mail-test.oriso.org", "logoUrl": "https://mail-test.oriso.org/service/tenant/public/branding/7/logo", "platformName": "Test platform"}}
    (chart / "values.yaml").write_text(json.dumps(values))
    rendered = subprocess.check_output(["helm", "template", "email-test", str(chart)], text=True)
    assert "name: ORISO_APP_URL" in rendered
    assert 'value: "https://mail-test.oriso.org"' in rendered
    assert 'value: "https://mail-test.oriso.org/datenschutz"' in rendered
    assert 'value: "https://mail-test.oriso.org/service/tenant/public/branding/7/logo"' in rendered
    assert "app.oriso.org" not in rendered
print("Keycloak mail environment render passed")
