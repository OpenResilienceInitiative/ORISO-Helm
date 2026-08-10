#!/usr/bin/env python3
"""Render invariant: the UserService MatrixRTC call-policy HMAC secret must never render empty.

Why this exists:
    ``MatrixRtcCorrelationIdHasher`` pseudonymizes Matrix room id / user id
    pairs before they are written to call-policy denial logs. Its
    ``@PostConstruct`` fails fast:

        if (secret == null || secret.isBlank())
          throw new IllegalStateException(
              "matrixrtc.call-policy.hmac-secret must be set "
              "(MATRIXRTC_CALL_POLICY_HMAC_SECRET)");

    ``application.properties`` maps the property to
    ``${MATRIXRTC_CALL_POLICY_HMAC_SECRET:}`` -- an EMPTY default -- so a
    UserService container that starts without the env var does not degrade,
    it exits 1 on every boot. That makes this a mandatory boot config, in the
    same class as ``STATISTICS_MESSAGE_COUNT_HMAC_SECRET`` and
    ``SERVICE_ENCRYPTION_APPKEY``.

    Marking the value ``required`` makes the deploy fail loudly instead of the
    pod failing quietly with a CrashLoopBackOff that points at the pod, not at
    the deploy (the AS-C01 failure mode from ORISO-Helm#49).

Rendering technique -- two passes, mirroring
``render_statistics_hmac_secret_test.py``:
    1. ISOLATED MINIMAL CHART for the ``required`` guard: a throwaway chart
       holding only the template under test, so a deliberately-missing value
       cannot be masked by the vendored subcharts.
    2. FULL CHART for the delivery path: values.yaml.default +
       secrets.yaml.default (+ values-pre-dev.yaml) must actually land the env
       var on the container, not just in the Secret.

Invariants asserted:
    1. With a secret supplied, ``MATRIXRTC_CALL_POLICY_HMAC_SECRET`` is
       present and base64-decodes back to exactly that secret.
    2. With the secret absent, rendering FAILS -- no silent empty-secret Secret.
    3. With the secret explicitly empty, rendering FAILS for the same reason.
    4. The Secret keeps its name and its other keys, so the guard cannot
       quietly drop an unrelated credential.
    5. The default values set (values.yaml.default + secrets.yaml.default)
       renders a NON-EMPTY secret and the Deployment imports it via
       ``secretKeyRef`` from ``userservice-secret``.
    6. The Pre-Dev overlay renders the same way -- values-pre-dev.yaml must not
       drop the key while overriding the UserService block.

Usage:  python3 tests/render_matrixrtc_call_policy_hmac_secret_test.py   (requires helm + pyyaml)
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
SENTINEL = "matrixrtc-call-policy-hmac-canary-not-a-real-secret"

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = "templates/userservice/userservice-secret.yaml"
SECRET_NAME = "userservice-secret"
ENV_KEY = "MATRIXRTC_CALL_POLICY_HMAC_SECRET"

# Every other key the template reads, so the render exercises the real file
# rather than dying on an unrelated nil.
BASE_OVERLAY = {
    "global": {
        "secrets": {
            "liquibaseUser": "test-liquibase-user",
            "liquibasePassword": "test-liquibase-pass",
            "rabbitmqUsername": "test-rabbit-user",
            "rabbitmqPassword": "test-rabbit-pass",
            "userServiceDbUsername": "test-db-user",
            "userServiceDbPassword": "test-db-pass",
            "keycloakAdminUsername": "test-kc-user",
            "keycloakAdminPassword": "test-kc-pass",
            "matrixRegistrationSharedSecret": "test-shared-secret",
        },
        "matrix": {
            "matrixAdminUsername": "test-matrix-admin",
            "matrixAdminPassword": "test-matrix-pass",
        },
    },
    "userService": {
        "serviceEncryptionAppkey": "test-appkey",
        "keycloakTechnicalUsername": "test-technical-user",
        "keycloakTechnicalPassword": "test-technical-pass",
        "identityTechnicalUserUsername": "test-identity-user",
        "identityTechnicalUserPassword": "test-identity-pass",
        "statisticsMessageCountHmacSecret": "test-statistics-hmac",
    },
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
            "name: matrixrtc-call-policy-hmac-render-test\n"
            "description: Isolated minimal chart rendering the UserService "
            "secret without subcharts.\n"
            "version: 0.0.0\n"
        )


def render_isolated(chart: str, hmac_secret):
    """Render the minimal chart with *hmac_secret*; pass ``None`` to omit it.

    Returns ``(returncode, stdout, stderr)`` -- a failed render is an expected
    outcome here, not a fatal error.
    """
    overlay = {
        "global": BASE_OVERLAY["global"],
        "userService": dict(BASE_OVERLAY["userService"]),
    }
    if hmac_secret is not None:
        overlay["userService"]["matrixRtcCallPolicyHmacSecret"] = hmac_secret
    ov = os.path.join(chart, "overlay.yaml")
    with open(ov, "w") as fh:
        yaml.safe_dump(overlay, fh)
    proc = subprocess.run(
        ["helm", "template", "matrixrtc-hmac", chart, "-f", ov],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def render_full_chart(*extra_values: str) -> list[dict]:
    """Render the real chart the way a deploy does, optionally with an overlay."""
    cmd = [
        "helm",
        "template",
        "matrixrtc-hmac-full",
        CHART_DIR,
        "-f",
        os.path.join(CHART_DIR, "values.yaml.default"),
        "-f",
        os.path.join(CHART_DIR, "secrets.yaml.default"),
    ]
    for values_file in extra_values:
        cmd += ["-f", os.path.join(CHART_DIR, values_file)]
    # The default values configure an SMTP transport, whose render gate requires
    # credentials; real deploys carry them in the persistent secret values.
    cmd += [
        "--set-string",
        "userService.smtpUser=smtp-canary-user",
        "--set-string",
        "userService.smtpPassword=smtp-canary-password",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        die(f"full-chart render failed for {extra_values or ('defaults',)}:\n{proc.stderr}")
    return [doc for doc in yaml.safe_load_all(proc.stdout) if isinstance(doc, dict)]


def secret_doc(docs, name: str = SECRET_NAME) -> dict:
    for doc in docs:
        if doc.get("kind") == "Secret" and doc.get("metadata", {}).get("name") == name:
            return doc
    die(f"no Secret named {name} rendered")


def userservice_deployment(docs) -> dict:
    for doc in docs:
        if doc.get("kind") == "Deployment" and "userservice" in doc["metadata"]["name"]:
            return doc
    die("UserService Deployment was not rendered")


def assert_delivered(label: str, *extra_values: str) -> None:
    """The env var must reach the container, non-empty, for this values set."""
    docs = render_full_chart(*extra_values)

    data = secret_doc(docs).get("data") or {}
    encoded = data.get(ENV_KEY)
    if check(encoded is not None, f"{label}: {SECRET_NAME} carries {ENV_KEY}"):
        decoded = base64.b64decode(encoded).decode()
        check(
            decoded.strip() != "",
            f"{label}: {ENV_KEY} is non-empty "
            f"(a blank value crashes UserService on boot)",
        )

    container = userservice_deployment(docs)["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e for e in container.get("env", [])}
    if check(ENV_KEY in env, f"{label}: UserService Deployment imports {ENV_KEY}"):
        ref = (env[ENV_KEY].get("valueFrom") or {}).get("secretKeyRef") or {}
        check(
            ref.get("name") == SECRET_NAME and ref.get("key") == ENV_KEY,
            f"{label}: {ENV_KEY} is secretKeyRef'd from {SECRET_NAME} "
            f"(not an inline plaintext value)",
        )
        check(
            "value" not in env[ENV_KEY],
            f"{label}: {ENV_KEY} is not set as an inline plaintext env value",
        )


def main() -> int:
    if shutil.which("helm") is None:
        die("helm is not on PATH")

    with tempfile.TemporaryDirectory() as tmp:
        chart = os.path.join(tmp, "chart")
        os.makedirs(chart)
        build_minimal_chart(chart)

        # 1 + 4: a supplied secret round-trips, and the Secret is otherwise intact.
        rc, stdout, stderr = render_isolated(chart, SENTINEL)
        if rc != 0:
            die(f"render with a valid hmac secret failed:\n{stderr}")
        doc = secret_doc(list(yaml.safe_load_all(stdout)))
        data = doc.get("data", {})
        encoded = data.get(ENV_KEY)
        if check(encoded is not None, f"{ENV_KEY} is rendered"):
            check(
                base64.b64decode(encoded).decode() == SENTINEL,
                f"{ENV_KEY} base64-decodes to the configured value",
            )
        check(doc["metadata"]["name"] == SECRET_NAME, f"Secret name is still {SECRET_NAME}")
        check(
            "SERVICE_ENCRYPTION_APPKEY" in data
            and "STATISTICS_MESSAGE_COUNT_HMAC_SECRET" in data,
            "pre-existing Secret keys are still rendered",
        )

        # 2: omitted entirely -- the environment whose secret values predate the key.
        rc, _, _ = render_isolated(chart, None)
        check(
            rc != 0,
            "render FAILS when matrixRtcCallPolicyHmacSecret is absent "
            "(no silent empty-secret Secret)",
        )

        # 3: present but blank -- same blast radius, different typo.
        rc, _, _ = render_isolated(chart, "")
        check(
            rc != 0,
            "render FAILS when matrixRtcCallPolicyHmacSecret is an empty string",
        )

    # 5 + 6: the values sets an operator actually deploys with.
    assert_delivered("default values")
    assert_delivered("values-pre-dev.yaml", "values-pre-dev.yaml")

    if _failures:
        print(f"\n{len(_failures)} check(s) failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
