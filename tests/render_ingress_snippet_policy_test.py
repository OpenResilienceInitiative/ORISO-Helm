#!/usr/bin/env python3
"""Exercise the snippet shutdown with a chart-sized fixture and check its inventory."""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = "nginx/ingress-nginx-controller-configmap.yaml"
POLICY = "nginx/_snippet-policy.tpl"
HELPERS = "_helpers.tpl"
SNIPPET = re.compile(r"""^[ \t]*["']?nginx\.ingress\.kubernetes\.io/[a-z-]+-snippet["']?[ \t]*:""", re.M)
HSTS = {
    "hsts": "true",
    "hsts-max-age": "31536000",
    "hsts-include-subdomains": "true",
    "hsts-preload": "false",
}
INGRESS = """
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: fixture-ingress
  {{- if .Values.fixtureSnippet }}
  annotations:
    nginx.ingress.kubernetes.io/configuration-snippet: "# synthetic annotation"
  {{- end }}
spec: {}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: second-fixture-ingress
  {{- if .Values.fixtureSecondSnippet }}
  annotations:
    "nginx.ingress.kubernetes.io/server-snippet": "# synthetic annotation"
  {{- end }}
spec: {}
"""


def render(chart, *settings, fail=False):
    command = ["helm", "template", "fixture", str(chart)]
    for setting in settings:
        command.extend(["--set", setting])
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if fail:
        assert result.returncode, result.stdout
        return result.stderr
    assert result.returncode == 0, result.stderr
    return {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in yaml.safe_load_all(result.stdout) if doc
    }


def main():
    inventory = (ROOT / "templates" / POLICY).read_text()
    template_names = set(re.findall(r'"([a-z/-]+\.yaml)"', inventory))
    for directory in ("templates", "charts"):
        for template in (ROOT / directory).rglob("*.yaml"):
            if SNIPPET.search(template.read_text()):
                relative = template.relative_to(ROOT / "templates") if directory == "templates" else template
                assert str(relative) in template_names, f"{relative} must be checked by the snippet policy"
    with tempfile.TemporaryDirectory(prefix="ingress-snippet-policy-") as directory:
        chart = Path(directory)
        (chart / "Chart.yaml").write_text("apiVersion: v2\nname: snippet-fixture\nversion: 0.1.0\n")
        (chart / "values.yaml").write_text("nginxController:\n  enabled: true\nipAnonymizer:\n  enabled: false\n")
        for name in template_names | {CONTROLLER, POLICY, HELPERS}:
            output = chart / "templates" / name
            output.parent.mkdir(parents=True, exist_ok=True)
            if name in (CONTROLLER, POLICY, HELPERS):
                shutil.copyfile(ROOT / "templates" / name, output)
            else:
                output.write_text(INGRESS if name == "nginx/main-ingress.yaml" else "")
        key = ("ConfigMap", "ingress-nginx-controller")
        legacy = render(chart, "fixtureSnippet=true")[key]["data"]
        assert legacy["allow-snippet-annotations"] == "true"
        assert legacy["annotations-risk-level"] == "Critical"
        for field, value in HSTS.items():
            assert legacy[field] == value
        enabled = "nginxController.disableSnippetAnnotations=true"
        active = render(chart, enabled)[key]["data"]
        assert active == {**legacy, "allow-snippet-annotations": "false", "annotations-risk-level": "High"}
        assert "fixture-ingress" in render(chart, enabled, "fixtureSnippet=true", fail=True)
        assert "second-fixture-ingress" in render(chart, enabled, "fixtureSecondSnippet=true", fail=True)
        assert key not in render(chart, enabled, "nginxController.enabled=false", "fixtureSnippet=true")
    print("PASS: shutdown rejects legacy snippets, preserves HSTS and changes only the bundled controller policy")


if __name__ == "__main__":
    main()
