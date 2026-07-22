#!/usr/bin/env python3
"""ADR-005 / DB-M04 render-based invariant tests for the Matrix identity config.

The Matrix ``server_name`` is immutable inside every user ID (``@alice:matrix.oriso.org``)
and room ID, so it must be a stable domain driven from a single value
(``.Values.matrix.matrixServerName``) and never a bare host IP. This suite renders
the Matrix-related templates and locks the invariants that a regression would break.

Rendering technique — ISOLATED MINIMAL CHART:
    ``helm template`` on the full chart fails because the vendored subcharts
    (rabbitmq/mongodb/redis) blow up on nil ``global.secrets.*`` and this chart
    ships no committed ``values.yaml``. We therefore build a throwaway chart that
    contains ONLY the templates under test plus a ``Chart.yaml`` with no
    dependencies, seed it with the committed ``values.yaml.default`` (which already
    supplies every non-secret key the service config-maps reference) and layer a
    tiny overlay that adds the two secrets and a distinctive sentinel server name.
    No subchart is ever evaluated, so the render is clean and hermetic.

Invariants asserted (task requirements 1-6):
    1. Rendered ``homeserver.yaml`` parses as valid YAML (block-scalar / whitespace
       trim guard — the real protection against a broken ``{{- range ... }}``).
    2. ``server_name`` equals the configured domain and is never a bare IPv4.
    3. Federation is closed: ``federation_domain_whitelist == []`` and
       ``allow_public_rooms_over_federation`` is false.
    4. ``exempt_from_ratelimiting`` is correct in BOTH branches of the optional
       ``serverPublicIp`` value.
    5. The well-known ``m.server`` values and the nginx discovery ``server_name``
       use the configured domain, not an IP.
    6. Single source of truth: every propagated ``MATRIX_SERVER_NAME`` (user/agency/
       tenant service config-maps) and the Element Call ``server_name`` equal
       ``matrix.matrixServerName``.

Usage:  python3 tests/render_adr005_test.py     (requires ``helm`` on PATH + pyyaml)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

# A distinctive, obviously-a-domain sentinel. Using a value that appears nowhere
# in the templates proves each rendered occurrence is chart-driven, not hardcoded:
# a hardcoded IP (the DB-M04 regression) or any divergent literal fails the checks.
SENTINEL = "matrix.adr005-canary.example"
PUBLIC_IP = "203.0.113.9"  # RFC 5737 documentation range — safe, never routable.

IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = [
    "templates/matrix/matrix-configmaps.yaml",
    "templates/userservice/userservice-configmap-env.yaml",
    "templates/agencyservice/agencyservice-configmap-env.yaml",
    "templates/tenantservice/tenantservice-configmap-env.yaml",
    "templates/element-call/element-call-configmap.yaml",
]

_failures: list[str] = []


def check(cond: bool, msg: str) -> bool:
    """Record and print a single assertion result. Returns the boolean outcome."""
    if cond:
        print(f"PASS: {msg}")
    else:
        print(f"FAIL: {msg}")
        _failures.append(msg)
    return bool(cond)


def die(msg: str) -> None:
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(2)


def build_minimal_chart(dst: str) -> None:
    """Assemble the isolated minimal chart under *dst*."""
    os.makedirs(os.path.join(dst, "templates"))
    with open(os.path.join(dst, "Chart.yaml"), "w") as fh:
        fh.write(
            "apiVersion: v2\n"
            "name: adr005-matrix-render-test\n"
            "description: Isolated minimal chart rendering the Matrix ADR-005 "
            "templates without subcharts.\n"
            "version: 0.0.0\n"
        )
    # The committed sample values provide every non-secret key the service
    # config-maps reference; the overlay adds only what is missing (secrets) and
    # overrides the two values ADR-005 cares about (server name + public IP).
    shutil.copyfile(
        os.path.join(CHART_DIR, "values.yaml.default"),
        os.path.join(dst, "values.yaml"),
    )
    for rel in TEMPLATES:
        src = os.path.join(CHART_DIR, rel)
        if not os.path.isfile(src):
            die(f"template under test is missing: {rel}")
        out = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.copyfile(src, out)


def render(chart: str, server_public_ip: str) -> str:
    """Render the minimal chart and return helm's raw stdout."""
    overlay = {
        "matrix": {"matrixServerName": SENTINEL, "serverPublicIp": server_public_ip},
        "global": {
            "secrets": {
                "redisdefaultPass": "test-redis-pass",
                "matrixRegistrationSharedSecret": "test-shared-secret",
            }
        },
    }
    ov = os.path.join(chart, "overlay.yaml")
    with open(ov, "w") as fh:
        yaml.safe_dump(overlay, fh)
    proc = subprocess.run(
        ["helm", "template", "adr005", chart, "-f", ov],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        die(f"helm template failed (serverPublicIp={server_public_ip!r}):\n{proc.stderr}")
    return proc.stdout


def configmaps(stdout: str) -> dict:
    """Parse helm output into {configmap name: document}. Raises on invalid YAML."""
    docs = [d for d in yaml.safe_load_all(stdout) if isinstance(d, dict)]
    return {
        d["metadata"]["name"]: d for d in docs if d.get("kind") == "ConfigMap"
    }


def parse_homeserver(cm: dict):
    """Return the parsed homeserver.yaml mapping, or None if it is not valid YAML."""
    raw = cm["matrix-homeserver-oidc"]["data"]["homeserver.yaml"]
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
    return doc if isinstance(doc, dict) else None


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="adr005-render-")
    try:
        chart = os.path.join(tmp, "chart")
        build_minimal_chart(chart)

        stdout_a = render(chart, "")          # serverPublicIp unset (empty == unset)
        stdout_b = render(chart, PUBLIC_IP)   # serverPublicIp provided

        # --- Requirement 1: the rendered config parses as valid YAML -------------
        # (a) the whole helm output, (b) the inner homeserver.yaml block scalar.
        try:
            cm_a = configmaps(stdout_a)
            cm_b = configmaps(stdout_b)
            outer_ok = True
        except yaml.YAMLError as exc:
            outer_ok = False
            print(f"  outer YAML parse error: {exc}")
        check(outer_ok, "rendered Matrix templates parse as valid YAML (outer documents)")
        if not outer_ok:
            finalize()

        hs_a = parse_homeserver(cm_a)
        hs_b = parse_homeserver(cm_b)
        check(hs_a is not None, "rendered homeserver.yaml parses as a valid YAML mapping")

        # --- Requirement 2: server_name is the configured domain, never IPv4 -----
        server_name = hs_a.get("server_name") if hs_a else None
        check(
            server_name == SENTINEL,
            f"homeserver server_name == configured matrixServerName ({SENTINEL!r}); got {server_name!r}",
        )
        check(
            isinstance(server_name, str) and not IPV4.match(server_name),
            f"homeserver server_name is a domain, not a bare IPv4; got {server_name!r}",
        )

        # --- Requirement 3: federation is closed ---------------------------------
        whitelist = hs_a.get("federation_domain_whitelist") if hs_a else "<no homeserver>"
        check(
            whitelist == [],
            f"federation_domain_whitelist == [] (federation closed); got {whitelist!r}",
        )
        allow_pub = hs_a.get("allow_public_rooms_over_federation") if hs_a else "<no homeserver>"
        check(
            allow_pub is False,
            f"allow_public_rooms_over_federation is false; got {allow_pub!r}",
        )

        # --- Requirement 4: exempt_from_ratelimiting, BOTH branches --------------
        exempt_a = hs_a.get("exempt_from_ratelimiting") if hs_a else None
        check(
            exempt_a == ["10.42.0.0/16", "127.0.0.1"],
            "exempt_from_ratelimiting (serverPublicIp unset) == ['10.42.0.0/16', "
            f"'127.0.0.1']; got {exempt_a!r}",
        )
        exempt_b = hs_b.get("exempt_from_ratelimiting") if hs_b else None
        check(
            exempt_b == ["10.42.0.0/16", PUBLIC_IP, "127.0.0.1"],
            f"exempt_from_ratelimiting (serverPublicIp={PUBLIC_IP}) == ['10.42.0.0/16', "
            f"'{PUBLIC_IP}', '127.0.0.1']; got {exempt_b!r}",
        )

        # --- Requirement 5: well-known m.server + nginx server_name use domain ---
        discovery = cm_a["matrix-discovery-data"]["data"]
        for key, port in (("caritas.local", 8448), ("caritas2.local", 8449)):
            m_server = json.loads(discovery[key])["m.server"]
            host = m_server.rsplit(":", 1)[0]
            check(
                m_server == f"{SENTINEL}:{port}",
                f"well-known {key} m.server == {SENTINEL}:{port}; got {m_server!r}",
            )
            check(
                not IPV4.match(host),
                f"well-known {key} m.server host is a domain, not a bare IPv4; got {host!r}",
            )
        nginx_conf = cm_a["matrix-discovery-config"]["data"]["default.conf"]
        match = re.search(r"server_name\s+([^;\s]+)\s*;", nginx_conf)
        nginx_sn = match.group(1) if match else None
        check(
            nginx_sn == SENTINEL,
            f"nginx discovery server_name == {SENTINEL!r}; got {nginx_sn!r}",
        )
        check(
            isinstance(nginx_sn, str) and not IPV4.match(nginx_sn),
            f"nginx discovery server_name is a domain, not a bare IPv4; got {nginx_sn!r}",
        )

        # --- Requirement 6: single source of truth -------------------------------
        propagated = {
            "userservice MATRIX_SERVER_NAME":
                cm_a["userservice-configmap-env"]["data"]["MATRIX_SERVER_NAME"],
            "agencyservice MATRIX_SERVER_NAME":
                cm_a["agencyservice-configmap-env"]["data"]["MATRIX_SERVER_NAME"],
            "tenantservice MATRIX_SERVER_NAME":
                cm_a["tenantservice-configmap-env"]["data"]["MATRIX_SERVER_NAME"],
            "element-call server_name":
                json.loads(cm_a["element-call-config"]["data"]["config.json"])
                ["default_server_config"]["m.homeserver"]["server_name"],
        }
        for label, value in propagated.items():
            check(
                value == SENTINEL,
                f"{label} == single source of truth ({SENTINEL!r}); got {value!r}",
            )

        finalize()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def finalize() -> None:
    if _failures:
        print(f"\n{len(_failures)} ADR-005 render assertion(s) FAILED", file=sys.stderr)
        sys.exit(1)
    print("\nAll ADR-005 render invariants hold.")
    sys.exit(0)


if __name__ == "__main__":
    main()
