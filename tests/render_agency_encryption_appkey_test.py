#!/usr/bin/env python3
"""AS-C01 render invariant: the AgencyService Matrix encryption key must never render empty.

Why this exists (ORISO-Helm#49):
    ``SERVICE_ENCRYPTION_APPKEY`` encrypts agency Matrix service-account passwords
    at rest. On the pre-dev cluster it existed only as an out-of-band
    ``kubectl patch`` on a Helm-owned Secret and was absent from the chart, so the
    next ``helm upgrade`` would have re-rendered the Secret without it.
    AgencyService then falls back to an empty ``service.encryption.appkey`` and
    ``AgencyMatrixPasswordCipher`` throws "Agency Matrix password encryption key
    is not configured" -- blocking agency creation and leaving every stored
    ``enc:`` credential undecryptable.

    PR #47 added the key to this chart, but as a plain lookup. ``nil | b64enc``
    renders an empty string, so an unset value still produces a *successful*
    upgrade that silently ships a Secret with a blank key -- the identical
    failure mode, just moved into this chart. The fix is to mark the value
    ``required`` so the deploy fails loudly instead.

Rendering technique -- ISOLATED MINIMAL CHART:
    Same approach as ``render_adr005_test.py``: ``helm template`` on the full
    chart fails because the vendored subcharts blow up on nil ``global.secrets.*``
    and this chart ships no committed ``values.yaml``. We build a throwaway chart
    containing only the template under test, so the render is hermetic.

Invariants asserted:
    1. With a key supplied, ``SERVICE_ENCRYPTION_APPKEY`` is present and
       base64-decodes back to exactly that key (guards the ``b64enc`` pipeline).
    2. With the key absent, rendering FAILS -- no silent empty-key Secret.
    3. With the key explicitly empty, rendering FAILS for the same reason.
    4. The Secret keeps its name and its other keys, so this change cannot
       quietly drop an unrelated credential.

Usage:  python3 tests/render_agency_encryption_appkey_test.py   (requires helm + pyyaml)
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import tempfile

import yaml

# Distinctive sentinel: appears nowhere in the templates, so a passing decode
# proves the value is chart-driven rather than hardcoded.
SENTINEL = "asc01-appkey-canary-not-a-real-secret"

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = "templates/agencyservice/agencyservice-secret.yaml"
SECRET_NAME = "agencyservice-secret"

# Every other key the template reads, so the render exercises the real file.
BASE_OVERLAY = {
    "global": {
        "secrets": {
            "liquibaseUser": "test-liquibase-user",
            "liquibasePassword": "test-liquibase-pass",
            "agencyServiceDbUsername": "test-db-user",
            "agencyServiceDbPassword": "test-db-pass",
            "keycloakAdminUsername": "test-kc-user",
            "keycloakAdminPassword": "test-kc-pass",
            "matrixRegistrationSharedSecret": "test-shared-secret",
        },
        "matrix": {
            "matrixAdminUsername": "test-matrix-admin",
            "matrixAdminPassword": "test-matrix-pass",
        },
    }
}

_failures: list[str] = []


def check(cond: bool, msg: str) -> bool:
    print(f"{'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        _failures.append(msg)
    return bool(cond)


def die(msg: str) -> None:
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(2)


def build_minimal_chart(dst: str) -> None:
    """Assemble a dependency-free chart containing only the template under test."""
    src = os.path.join(CHART_DIR, TEMPLATE)
    if not os.path.isfile(src):
        die(f"template under test is missing: {TEMPLATE}")
    out = os.path.join(dst, TEMPLATE)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    shutil.copyfile(src, out)
    with open(os.path.join(dst, "Chart.yaml"), "w") as fh:
        fh.write(
            "apiVersion: v2\n"
            "name: asc01-appkey-render-test\n"
            "description: Isolated minimal chart rendering the AgencyService "
            "secret without subcharts.\n"
            "version: 0.0.0\n"
        )


def render(chart: str, appkey):
    """Render with *appkey*; pass ``None`` to omit it entirely.

    Returns ``(returncode, stdout, stderr)`` -- a failed render is an expected
    outcome here, not a fatal error.
    """
    overlay = dict(BASE_OVERLAY)
    if appkey is not None:
        overlay = {**BASE_OVERLAY, "agencyService": {"serviceEncryptionAppkey": appkey}}
    ov = os.path.join(chart, "overlay.yaml")
    with open(ov, "w") as fh:
        yaml.safe_dump(overlay, fh)
    proc = subprocess.run(
        ["helm", "template", "asc01", chart, "-f", ov],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def secret_doc(stdout: str) -> dict:
    for doc in yaml.safe_load_all(stdout):
        if isinstance(doc, dict) and doc.get("kind") == "Secret":
            return doc
    die("no Secret rendered")


def main() -> int:
    if shutil.which("helm") is None:
        die("helm is not on PATH")

    with tempfile.TemporaryDirectory() as tmp:
        chart = os.path.join(tmp, "chart")
        os.makedirs(chart)
        build_minimal_chart(chart)

        # 1 + 4: a supplied key round-trips, and the Secret is otherwise intact.
        rc, stdout, stderr = render(chart, SENTINEL)
        if rc != 0:
            die(f"render with a valid appkey failed:\n{stderr}")
        doc = secret_doc(stdout)
        data = doc.get("data", {})
        encoded = data.get("SERVICE_ENCRYPTION_APPKEY")
        if check(encoded is not None, "SERVICE_ENCRYPTION_APPKEY is rendered"):
            decoded = base64.b64decode(encoded).decode()
            check(
                decoded == SENTINEL,
                "SERVICE_ENCRYPTION_APPKEY base64-decodes to the configured value",
            )
        check(
            doc["metadata"]["name"] == SECRET_NAME,
            f"Secret name is still {SECRET_NAME}",
        )
        check(
            "MATRIX_ADMIN_PASSWORD" in data and "SPRING_LIQUIBASE_USER" in data,
            "pre-existing Secret keys are still rendered",
        )

        # 2: omitted entirely -- the original #49 failure mode.
        rc, _, stderr = render(chart, None)
        check(
            rc != 0,
            "render FAILS when serviceEncryptionAppkey is absent "
            "(no silent empty-key Secret)",
        )

        # 3: present but blank -- same blast radius, different typo.
        rc, _, stderr = render(chart, "")
        check(
            rc != 0,
            "render FAILS when serviceEncryptionAppkey is an empty string",
        )

    if _failures:
        print(f"\n{len(_failures)} check(s) failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
