#!/usr/bin/env python3
"""Five-actor native Keycloak fixture and opt-in receiving HTTP test runner.

The fixture is synthetic and ephemeral. It imports no legacy task registry,
reconciler or custom command provider. Output contains counts/source hashes only.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import hashlib
import json
import math
import os
import re
from pathlib import Path
import sys
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
import uuid
import xml.etree.ElementTree as XML


class GateError(Exception):
    pass


def executed_counts(receiver, started, expected_tests=5):
    if not math.isfinite(started) or started <= 0 or expected_tests <= 0:
        raise GateError(
            "A finite positive start timestamp and positive case count are required"
        )
    reports = list(
        (receiver / "target/failsafe-reports").glob(
            "TEST-*RealTaskTokenAuthorizationIT.xml"
        )
    )
    if len(reports) != 1 or reports[0].stat().st_mtime < started:
        raise GateError("A fresh single-suite Failsafe execution report is required")
    try:
        report = XML.parse(reports[0]).getroot()
        counts = {
            k: int(report.get(k, "0"))
            for k in ("tests", "failures", "errors", "skipped")
        }
    except (ValueError, XML.ParseError):
        raise GateError("Invalid Failsafe execution report") from None
    if counts["tests"] != expected_tests or any(
        counts[k] for k in ("failures", "errors", "skipped")
    ):
        raise GateError(
            "Native receiver must execute its exact expected cases with zero failures, errors or skips"
        )
    return counts


ACTORS = (
    (
        "SETUP",
        "backend-setup",
        ("config-wizard", "invitation-reservations", "consultant-import"),
        ("tenantservice", "agencyservice", "consultingtypeservice", "userservice"),
    ),
    (
        "ACCOUNT_CREATE",
        "backend-account-create",
        ("account-provisioning", "account-read"),
        ("userservice",),
    ),
    (
        "ACCOUNT_ADMIN",
        "backend-account-admin",
        ("account-maintenance", "account-read", "otp-config-admin", "session-exchange"),
        ("userservice", "oriso-otp"),
    ),
    (
        "MAIL",
        "backend-mail",
        (
            "smtp-sync",
            "system-email-delivery",
            "notification-dispatch",
            "notifications-technical",
        ),
        ("tenantservice", "agencyservice", "consultingtypeservice", "userservice"),
    ),
    (
        "RUNTIME",
        "backend-runtime",
        (
            "runtime-policy",
            "matrix-agency",
            "matrix-agency-provision",
            "appointment-sync",
            "appointment-participant-cleanup",
            "deletion-audit-read",
        ),
        ("tenantservice", "agencyservice", "appointmentservice"),
    ),
)


def native_request(base, method, path, body=None, token=None, form=False):
    import _keycloak_container as shared

    try:
        return shared.request(base, method, path, body, token, form=form, timeout=30)
    except HTTPError as error:
        code = error.code
        error.close()
        raise GateError(f"Native fixture Admin HTTP {method} returned {code}") from None


def claims(token):
    import _keycloak_container as shared

    return shared.claims(token)


def audiences(token_claims):
    value = token_claims.get("aud", [])
    return {value} if isinstance(value, str) else set(value)


def issue(base, realm, actor):
    return native_request(
        base,
        "POST",
        "/realms/" + realm + "/protocol/openid-connect/token",
        {
            "grant_type": "client_credentials",
            "client_id": actor["clientId"],
            "client_secret": actor["secret"],
        },
        form=True,
    )["access_token"]


def validate_token(token, actor, issuer):
    value = claims(token)
    if (
        value.get("iss") != issuer
        or value.get("azp") != actor["clientId"]
        or value.get("sub") != actor["subject"]
    ):
        raise GateError(
            "Native actor token binding differs from its actual service account"
        )
    if set(value.get("realm_access", {}).get("roles", [])) != set(
        actor["roles"]
    ) or value.get("resource_access"):
        raise GateError("Native actor token has unexpected realm/client grants")
    if audiences(value) != set(actor["audiences"]):
        raise GateError(
            "Native actor token audience differs from its receiver contract"
        )


def audience_mapper(name):
    return {
        "name": "receiver-" + name,
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "config": {
            "included.custom.audience": name,
            "access.token.claim": "true",
            "id.token.claim": "false",
        },
    }


def seed_actors(base, realm, admin):
    root = "/admin/realms/" + realm
    role_names = sorted(
        {r for _, _, roles, _ in ACTORS for r in roles} | {"unrelated-test-grant"}
    )
    native_request(
        base,
        "POST",
        "/admin/realms",
        {
            "realm": realm,
            "enabled": True,
            "accessTokenLifespan": 1800,
            "roles": {"realm": [{"name": r} for r in role_names]},
        },
        admin,
    )
    basics = [
        s
        for s in native_request(base, "GET", root + "/client-scopes", token=admin)
        if s["name"] == "basic"
    ]
    if len(basics) != 1:
        raise GateError("Native basic subject scope unavailable")
    roles = {
        r: native_request(
            base, "GET", root + "/roles/" + quote(r, safe=""), token=admin
        )
        for r in role_names
    }
    actors = []
    for key, client_id, grants, targets in ACTORS:
        secret = "synthetic-c-fixture-" + uuid.uuid4().hex + "-" + key.lower()
        role_mapper = {
            "name": "actor-realm-roles",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-realm-role-mapper",
            "config": {
                "claim.name": "realm_access.roles",
                "multivalued": "true",
                "jsonType.label": "String",
                "access.token.claim": "true",
                "id.token.claim": "false",
                "usermodel.realmRoleMapping.rolePrefix": "",
            },
        }
        native_request(
            base,
            "POST",
            root + "/clients",
            {
                "clientId": client_id,
                "protocol": "openid-connect",
                "publicClient": False,
                "secret": secret,
                "serviceAccountsEnabled": True,
                "enabled": True,
                "standardFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "fullScopeAllowed": False,
                "defaultClientScopes": ["basic"],
                "optionalClientScopes": [],
                "protocolMappers": [role_mapper]
                + [audience_mapper(a) for a in targets],
            },
            admin,
        )
        client = native_request(
            base,
            "GET",
            root + "/clients?" + urlencode({"clientId": client_id}),
            token=admin,
        )[0]
        path = root + "/clients/" + client["id"]
        for scope in native_request(
            base, "GET", path + "/default-client-scopes", token=admin
        ):
            if scope["id"] != basics[0]["id"]:
                native_request(
                    base,
                    "DELETE",
                    path + "/default-client-scopes/" + scope["id"],
                    token=admin,
                )
        native_request(
            base, "PUT", path + "/default-client-scopes/" + basics[0]["id"], token=admin
        )
        for scope in native_request(
            base, "GET", path + "/optional-client-scopes", token=admin
        ):
            native_request(
                base,
                "DELETE",
                path + "/optional-client-scopes/" + scope["id"],
                token=admin,
            )
        # A synthetic account may be created here; there is no read-only preview claim.
        account = native_request(
            base, "GET", path + "/service-account-user", token=admin
        )
        user_path = root + "/users/" + account["id"] + "/role-mappings/realm"
        defaults = native_request(base, "GET", user_path, token=admin)
        if defaults:
            native_request(base, "DELETE", user_path, defaults, admin)
        assigned = [roles[r] for r in grants]
        native_request(base, "POST", user_path, assigned, admin)
        native_request(base, "POST", path + "/scope-mappings/realm", assigned, admin)
        actors.append(
            {
                "key": key,
                "clientId": client_id,
                "subject": account["id"],
                "secret": secret,
                "roles": list(grants),
                "audiences": list(targets),
            }
        )
    return actors


def validate_native_accounts(base, realm, admin, actors):
    root = "/admin/realms/" + realm
    clients = native_request(base, "GET", root + "/clients", token=admin)
    actor_clients = {c["clientId"] for c in clients if c.get("serviceAccountsEnabled")}
    if actor_clients != {a["clientId"] for a in actors}:
        raise GateError("Native realm contains unexpected service-account clients")
    for actor in actors:
        user = root + "/users/" + actor["subject"]
        effective = native_request(
            base, "GET", user + "/role-mappings/realm/composite", token=admin
        )
        if {r["name"] for r in effective} != set(actor["roles"]):
            raise GateError("Native account has unexpected effective realm grants")
        if native_request(base, "GET", user + "/groups", token=admin):
            raise GateError("Native actor fixture unexpectedly depends on groups")
        for client in clients:
            mappings = native_request(
                base,
                "GET",
                user + "/role-mappings/clients/" + client["id"] + "/composite",
                token=admin,
            )
            if mappings:
                raise GateError(
                    "Native account has unexpected effective client administration grants"
                )


def real_negative_variants(base, realm, admin, actors):
    """Port of the retained public-native sampler; every mutation restored before yield."""
    root = "/admin/realms/" + realm
    unrelated = native_request(
        base, "GET", root + "/roles/unrelated-test-grant", token=admin
    )
    variants = {}
    issuer = base + "/realms/" + realm
    for actor in actors:
        client = native_request(
            base,
            "GET",
            root + "/clients?" + urlencode({"clientId": actor["clientId"]}),
            token=admin,
        )[0]
        path = root + "/clients/" + client["id"]
        basic = native_request(
            base, "GET", path + "/default-client-scopes", token=admin
        )
        if len(basic) != 1 or basic[0]["name"] != "basic":
            raise GateError("Unexpected actor default scopes")
        basic_path = path + "/default-client-scopes/" + basic[0]["id"]
        user_path = root + "/users/" + actor["subject"] + "/role-mappings/realm"
        scoped = path + "/scope-mappings/realm"
        try:
            native_request(base, "POST", user_path, [unrelated], admin)
            native_request(base, "POST", scoped, [unrelated], admin)
            mixed = issue(base, realm, actor)
            if set(claims(mixed).get("realm_access", {}).get("roles", [])) != set(
                actor["roles"]
            ) | {"unrelated-test-grant"}:
                raise GateError("Native extra-role variant was not actually issued")
        finally:
            native_request(base, "DELETE", user_path, [unrelated], admin)
            native_request(base, "DELETE", scoped, [unrelated], admin)
        wrong_audience = copy.deepcopy(client)
        wrong_audience["protocolMappers"] = [
            m
            for m in client.get("protocolMappers", [])
            if m["protocolMapper"] != "oidc-audience-mapper"
        ]
        wrong_audience["protocolMappers"].append(audience_mapper("unrelated-receiver"))
        try:
            native_request(base, "PUT", path, wrong_audience, admin)
            native_request(base, "PUT", basic_path, token=admin)
            audience = issue(base, realm, actor)
            if (
                audiences(claims(audience)) != {"unrelated-receiver"}
                or claims(audience).get("sub") != actor["subject"]
            ):
                raise GateError("Native wrong-audience variant was not actually issued")
        finally:
            native_request(base, "PUT", path, client, admin)
            native_request(base, "PUT", basic_path, token=admin)
        wrong_subject = copy.deepcopy(client)
        wrong_subject["protocolMappers"].append(
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
        try:
            native_request(base, "PUT", path, wrong_subject, admin)
            native_request(base, "DELETE", basic_path, token=admin)
            subject = issue(base, realm, actor)
            if claims(subject).get("sub") != "unbound-synthetic-subject":
                raise GateError("Native wrong-subject variant was not actually issued")
        finally:
            native_request(base, "PUT", path, client, admin)
            native_request(base, "PUT", basic_path, token=admin)
        validate_token(issue(base, realm, actor), actor, issuer)
        variants[actor["key"]] = {
            "mixedRoles": mixed,
            "wrongAudience": audience,
            "wrongSubject": subject,
        }
    return variants


@contextmanager
def fixture_file():
    """Yield only an ephemeral private file; all material and container cleaned on exit."""
    import _keycloak_container as shared

    container = shared.KeycloakContainer("five-actor-receivers")
    realm = "five-actors-" + uuid.uuid4().hex[:10]
    container.start()
    try:
        admin = shared.master_admin(container.base)
        actors = seed_actors(container.base, realm, admin)
        issuer = container.base + "/realms/" + realm
        validate_native_accounts(container.base, realm, admin, actors)
        for actor in actors:
            validate_token(issue(container.base, realm, actor), actor, issuer)
        variants = real_negative_variants(container.base, realm, admin, actors)
        validate_native_accounts(container.base, realm, admin, actors)
        native_request(
            container.base, "GET", "/realms/" + realm + "/protocol/openid-connect/certs"
        )
        with tempfile.TemporaryDirectory(
            prefix="oriso-five-actor-fixture-"
        ) as directory:
            path = (Path(directory) / "synthetic-five-actor-fixture.json").resolve()
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as output:
                json.dump(
                    {
                        "identityModel": "FIVE_ACTORS",
                        "issuer": issuer,
                        "tasks": actors,
                        "variants": variants,
                    },
                    output,
                )
            yield path
    finally:
        container.stop()


def validate_receiver(receiver):
    sources = list((receiver / "src/test").rglob("RealTaskTokenAuthorizationIT.java"))
    if len(sources) != 1:
        raise GateError("Receiver native test source missing or ambiguous")
    source = sources[0].read_text()
    if "FIVE_ACTORS" not in source:
        raise GateError("Receiver native tests do not support the five-actor fixture")
    pom = receiver / "pom.xml"
    ns = {"m": "http://maven.apache.org/POM/4.0.0"}
    if not any(
        p.findtext("m:id", namespaces=ns) == "native-permission-matrix"
        for p in XML.parse(pom).getroot().findall("m:profiles/m:profile", ns)
    ):
        raise GateError("Receiver native-permission-matrix profile is required")
    tracked_diff = subprocess.run(
        ["git", "diff", "HEAD", "--", "."],
        cwd=receiver,
        capture_output=True,
        check=True,
    ).stdout
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=receiver,
            capture_output=True,
            check=True,
        ).stdout
    )
    return {
        "worktreeDirty": dirty,
        "trackedDiffSha256": hashlib.sha256(tracked_diff).hexdigest(),
        "sourceSha256": hashlib.sha256(sources[0].read_bytes()).hexdigest(),
        "pomSha256": hashlib.sha256(pom.read_bytes()).hexdigest(),
        "gitHead": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=receiver,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip(),
    }


def run_receivers(receivers, fixture, java_home=None, expected_tests=5):
    evidence = []
    for receiver in receivers:
        provenance = validate_receiver(receiver)
        print(
            json.dumps(
                {
                    "receiverStart": receiver.name,
                    "expectedTests": expected_tests,
                    **provenance,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        environment = {**os.environ, "ORISO_TASK_TOKEN_FIXTURE": str(fixture)}
        if java_home:
            java_home = java_home.resolve()
            environment["JAVA_HOME"] = str(java_home)
            environment["PATH"] = (
                str(java_home / "bin") + os.pathsep + environment["PATH"]
            )
        command = [
            "./mvnw" if (receiver / "mvnw").is_file() else "mvn",
            "-q",
            "-Pnative-permission-matrix",
            "-Dit.test=RealTaskTokenAuthorizationIT",
            "verify",
        ]
        # Raw logs stay inside the same private temporary lifetime as synthetic tokens.
        log = fixture.parent / f"{receiver.name}-{uuid.uuid4().hex}-maven.log"
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        started = time.time()
        with os.fdopen(descriptor, "w") as output:
            result = subprocess.run(
                command,
                cwd=receiver,
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        if result.returncode:
            material = json.loads(fixture.read_text())
            safe = []
            for line in log.read_text(errors="replace").splitlines():
                if line.startswith("[ERROR]"):
                    for actor in material["tasks"]:
                        line = line.replace(
                            actor["secret"], "[redacted synthetic secret]"
                        )
                    line = re.sub(
                        r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
                        "[redacted JWT]",
                        line,
                    )
                    safe.append(line[:500])
            print(
                json.dumps(
                    {"receiverFailure": receiver.name, "diagnostics": safe[-10:]},
                    sort_keys=True,
                ),
                flush=True,
            )
            raise GateError("Native Maven receiver execution failed: " + receiver.name)
        counts = executed_counts(receiver, started, expected_tests)
        evidence.append({"receiver": receiver.name, **provenance, **counts})
        print(json.dumps({"receiverResult": evidence[-1]}, sort_keys=True), flush=True)
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--verify-report", type=Path)
    mode.add_argument("--verify-fixture-only", action="store_true")
    mode.add_argument("--receiver", action="append", type=Path)
    parser.add_argument("--started", type=float)
    parser.add_argument("--expected-tests", type=int, default=5)
    parser.add_argument("--java-home", type=Path)
    args = parser.parse_args()
    try:
        if args.verify_report:
            if args.started is None:
                raise GateError("--verify-report requires a start timestamp")
            print(
                json.dumps(
                    executed_counts(
                        args.verify_report, args.started, args.expected_tests
                    ),
                    sort_keys=True,
                )
            )
        else:
            if args.receiver:
                for receiver in args.receiver:
                    validate_receiver(receiver.resolve())
            with fixture_file() as fixture:
                if args.verify_fixture_only:
                    payload = json.loads(fixture.read_text())
                    print(
                        json.dumps(
                            {
                                "identityModel": payload["identityModel"],
                                "actorCount": len(payload["tasks"]),
                                "variantCount": sum(
                                    len(v) for v in payload["variants"].values()
                                ),
                                "actorKeys": [a["key"] for a in payload["tasks"]],
                                "nativePermissionsConfigured": False,
                            },
                            sort_keys=True,
                        )
                    )
                else:
                    run_receivers(
                        [r.resolve() for r in args.receiver],
                        fixture,
                        args.java_home,
                        args.expected_tests,
                    )
        return 0
    except GateError as error:
        print(json.dumps({"error": str(error)}), flush=True)
        return 1
    except (OSError, ValueError, XML.ParseError):
        # Native/test bodies can carry bearer material. Never print their exception payloads.
        print(
            json.dumps(
                {"error": "Five-actor fixture/native receiver verification failed"}
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
