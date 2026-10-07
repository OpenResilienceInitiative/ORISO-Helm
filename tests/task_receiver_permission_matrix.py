#!/usr/bin/env python3
"""Join actual rendered task JWTs to opt-in native receiver HTTP tests."""

import argparse
import copy
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import time
import xml.etree.ElementTree as XML
from keycloak_task_migration_test import claims, isolated_keycloak, rendered_contract, request


def executed_counts(receiver, started):
    reports = list((receiver / "target/surefire-reports").glob("TEST-*RealTaskTokenAuthorizationIT.xml"))
    if len(reports) != 1 or reports[0].stat().st_mtime < started:
        raise RuntimeError("Receiver gate produced no fresh single-suite execution report: " + receiver.name)
    report = XML.parse(reports[0]).getroot()
    counts = {key: int(report.get(key, "0")) for key in ("tests", "failures", "errors", "skipped")}
    if counts["tests"] < 1 or any(counts[key] for key in ("failures", "errors", "skipped")):
        raise RuntimeError("Receiver gate must execute cases with zero failures/errors/skips: " + receiver.name)
    return counts


def issue(base, realm, task):
    return request(
        base,
        "POST",
        "/realms/" + realm + "/protocol/openid-connect/token",
        {"grant_type": "client_credentials", "client_id": task["clientId"], "client_secret": task["secret"]},
        form=True,
    )["access_token"]


def real_negative_variants(base, realm, admin, tasks):
    """Native signed tokens with one deliberate binding/grant fault, never forged JSON."""
    root = "/admin/realms/" + realm
    request(base, "POST", root + "/roles", {"name": "unrelated-test-grant", "composite": False}, admin)
    unrelated = request(base, "GET", root + "/roles/unrelated-test-grant", token=admin)
    variants = {}
    for task in tasks:
        client = request(base, "GET", root + "/clients?clientId=" + task["clientId"], token=admin)[0]
        path = root + "/clients/" + client["id"]
        basic = request(base, "GET", path + "/default-client-scopes", token=admin)
        assert len(basic) == 1 and basic[0]["name"] == "basic"
        basicpath = path + "/default-client-scopes/" + basic[0]["id"]
        userpath = root + "/users/" + task["subject"] + "/role-mappings/realm"
        scoped = path + "/scope-mappings/realm"
        # The fault is a real extra effective realm role, propagated through an explicit scope.
        request(base, "POST", userpath, [unrelated], admin)
        request(base, "POST", scoped, [unrelated], admin)
        mixed = issue(base, realm, task)
        assert "unrelated-test-grant" in claims(mixed)["realm_access"]["roles"]
        request(base, "DELETE", userpath, [unrelated], admin)
        request(base, "DELETE", scoped, [unrelated], admin)
        wrongaud = copy.deepcopy(client)
        wrongaud["protocolMappers"] = [
            m for m in client.get("protocolMappers", []) if m["protocolMapper"] != "oidc-audience-mapper"
        ]
        wrongaud["protocolMappers"].append(
            {
                "name": "synthetic-wrong-audience",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "config": {
                    "included.custom.audience": "unrelated-receiver",
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                },
            }
        )
        request(base, "PUT", path, wrongaud, admin)
        request(base, "PUT", basicpath, token=admin)
        audience = issue(base, realm, task)
        assert claims(audience).get("aud") == "unrelated-receiver"
        assert claims(audience).get("sub") == task["subject"]
        request(base, "PUT", path, client, admin)
        request(base, "PUT", basicpath, token=admin)
        # basic owns the native subject mapper. Replace only in this isolated fixture.

        wrongsub = copy.deepcopy(client)
        wrongsub["protocolMappers"].append(
            {
                "name": "synthetic-wrong-subject",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-hardcoded-claim-mapper",
                "config": {
                    "claim.name": "sub",
                    "claim.value": "unbound-synthetic-subject",
                    "jsonType.label": "String",
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                },
            }
        )
        request(base, "PUT", path, wrongsub, admin)
        request(base, "PUT", basicpath, token=admin)
        request(base, "DELETE", basicpath, token=admin)
        subject = issue(base, realm, task)
        assert claims(subject).get("sub") == "unbound-synthetic-subject"
        request(base, "PUT", path, client, admin)
        request(base, "PUT", basicpath, token=admin)
        restored = claims(issue(base, realm, task))
        assert restored["sub"] == task["subject"]
        assert set(restored["realm_access"]["roles"]) == set(task["roles"])
        variants[task["key"]] = {"mixedRoles": mixed, "wrongAudience": audience, "wrongSubject": subject}
    return variants


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--receiver",
        action="append",
        type=Path,
        required=True,
        help="Existing receiver worktree containing RealTaskTokenAuthorizationIT",
    )
    parser.add_argument("--java-home", type=Path, help="Pinned supported JDK for the receiver build")
    args = parser.parse_args()
    realm, tasks = rendered_contract()
    realm["realm"] = "task-receiver-fixture"
    # Isolated native fixture: remove ORISO authenticators, retain task bindings.
    for key in (
        "authenticationFlows",
        "authenticatorConfig",
        "browserFlow",
        "directGrantFlow",
        "defaultRequiredActions",
    ):
        realm.pop(key, None)
    with isolated_keycloak() as (base, admin):
        request(base, "POST", "/admin/realms", realm, admin, timeout=90)
        spec = importlib.util.spec_from_file_location(
            "task_reconcile", Path(__file__).resolve().parents[1] / "files/keycloak-reconcile-task-identities.py"
        )
        reconcile = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reconcile)
        reconcile.reconcile(base, realm["realm"], admin, tasks)
        issuer = base + "/realms/" + realm["realm"]
        request(base, "GET", "/realms/" + realm["realm"] + "/protocol/openid-connect/certs", timeout=30)
        variants = real_negative_variants(base, realm["realm"], admin, tasks)
        with tempfile.TemporaryDirectory(prefix="oriso-receiver-fixture-") as directory:
            path = Path(directory) / "synthetic-task-bindings.json"
            path.write_text(json.dumps({"issuer": issuer, "tasks": tasks, "variants": variants}))
            path.chmod(0o600)
            environment = {**os.environ, "ORISO_TASK_TOKEN_FIXTURE": str(path)}
            if args.java_home:
                environment["JAVA_HOME"] = str(args.java_home)
                environment["PATH"] = str(args.java_home / "bin") + os.pathsep + environment["PATH"]
            failed_receivers = []
            for receiver in args.receiver:
                files = list((receiver / "src/test").rglob("RealTaskTokenAuthorizationIT.java"))
                if len(files) != 1:
                    raise RuntimeError("Receiver real-token test class missing or ambiguous: " + receiver.name)
                print("RUN: actual issued-token HTTP receiver " + receiver.name, flush=True)
                log_path = Path(tempfile.gettempdir()) / ("oriso-367-real-token-" + receiver.name + ".log")
                started = time.time()
                with log_path.open("w+b") as log:
                    log_path.chmod(0o600)
                    result = subprocess.run(
                        [
                            "./mvnw" if (receiver / "mvnw").is_file() else "mvn",
                            "-q",
                            "-Dtest=RealTaskTokenAuthorizationIT",
                            "test",
                        ],
                        cwd=receiver,
                        env=environment,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                    if result.returncode:
                        # Test logs can include HTTP DTOs. Keep raw diagnostics local;
                        # report only counts/class/exception lines, never bearer/material.
                        log.seek(0)
                        safe = []
                        for line in log.read().decode("utf8", "replace").splitlines():
                            if (
                                "Tests run:" in line
                                or line.startswith("[ERROR] Tests")
                                or line.startswith("[ERROR] Errors:")
                                or line.startswith("[ERROR] Failures:")
                            ):
                                safe.append(line[:500])
                        print("\n".join(safe[-12:]), flush=True)
                        failed_receivers.append(receiver.name)
                        continue
                counts = executed_counts(receiver, started)
                print("PASS: " + receiver.name + " " + json.dumps(counts, sort_keys=True), flush=True)
            if failed_receivers:
                raise RuntimeError("Real task-token receiving endpoint checks failed: " + ", ".join(failed_receivers))


if __name__ == "__main__":
    main()
