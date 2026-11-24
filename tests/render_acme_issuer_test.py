#!/usr/bin/env python3
"""Render the managed issuer with explicit contact and stable account identity."""

import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ISSUER_TEMPLATE = ROOT / "templates/nginx/issuer-production.yaml"
CONTACT = "acme+fixture@example.invalid"


def render(chart, *arguments, fail=False):
    result = subprocess.run(
        ["helm", "template", "acme-fixture", str(chart), *arguments],
        capture_output=True, text=True, check=False,
    )
    if fail:
        assert result.returncode != 0, "managed issuer rendered with an invalid configured contact"
        return result.stderr
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def main():
    with tempfile.TemporaryDirectory(prefix="acme-issuer-render-") as directory:
        chart = Path(directory)
        (chart / "templates").mkdir()
        (chart / "Chart.yaml").write_text("apiVersion: v2\nname: issuer-fixture\nversion: 0.1.0\n")
        defaults = yaml.safe_load((ROOT / "values.yaml.default").read_text())
        config = defaults["cert-manager"]
        assert config.get("acmeEmail", "") == ""
        (chart / "values.yaml").write_text(yaml.safe_dump({"cert-manager": config}))
        shutil.copyfile(ISSUER_TEMPLATE, chart / "templates/issuer.yaml")

        # Unset contact (Frank, 23 Nov 2026): the issuer renders without an email instead of failing,
        # so an offline/GitOps render without the value keeps working.
        for arguments in ((), ("--is-upgrade",)):
            [issuer] = render(chart, *arguments)
            assert "email" not in issuer["spec"]["acme"]
            assert issuer["spec"]["acme"]["privateKeySecretRef"] == {"name": "letsencrypt-prod"}
        assert "cert-manager.acmeEmail" in render(chart, "--set", "cert-manager.acmeEmail=true", fail=True)
        assert "cert-manager.acmeEmail" in render(chart, "--set-string", "cert-manager.acmeEmail=invalid", fail=True)
        for bad in ("ops@example.org/path", "ops@localhost", "ops@exa mple.org", "ops@-example.org", "ops@example-.org"):
            assert "cert-manager.acmeEmail" in render(chart, "--set-string", f"cert-manager.acmeEmail={bad}", fail=True)
        assert render(chart, "--set", "cert-manager.enabled=false") == []

        [issuer] = render(chart, "--set-string", f"cert-manager.acmeEmail={CONTACT}")
        assert (issuer["kind"], issuer["metadata"]["name"]) == ("ClusterIssuer", "letsencrypt-prod")
        assert issuer["spec"]["acme"] == {
            "email": CONTACT,
            "server": "https://acme-v02.api.letsencrypt.org/directory",
            "privateKeySecretRef": {"name": "letsencrypt-prod"},
            "solvers": [{"http01": {"ingress": {"class": "nginx"}}}],
        }
    print("PASS: managed ACME issuer uses a configured contact when set and retains its account key reference")


if __name__ == "__main__":
    main()
