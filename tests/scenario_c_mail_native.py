#!/usr/bin/env python3
"""Finite joined native MAIL gate. Requires explicit source trees and Java21 runtime."""

import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
import xml.etree.ElementTree as XML

ROOT = Path(__file__).resolve().parents[1]
REDACT = []
EVIDENCE = None
STAGE = "arguments"
RESOURCE_SUPPORT = None
MEMORY_LEASE = None
MONGO_IMAGE = "mongo@sha256:d341a86584b96eb665345a8f5b35fba8695ee1d0618fd012ec4696223a3d6c62"
EXPECTED_CASES = frozenset([
    "sameNativeBrokerBearerReadsRealSavedCtsAppliesSmtpAndAcknowledgesExactRevision",
    *["realNativeFiveAudienceMailIsAcceptedByTheActualBroker(TaskPurpose)[" + str(i) + "]"
      for i in range(1, 4)],
])


def ensure_headroom():
    if MEMORY_LEASE is not None:
        MEMORY_LEASE.check()
        return
    if sys.platform == "darwin":
        report = subprocess.check_output(["memory_pressure", "-Q"], text=True)
        match = re.search(r"memory free percentage:\s*(\d+)%", report)
        if match is None or int(match[1]) < 25:
            raise RuntimeError("Native MAIL requires at least25 percent memory headroom")
    elif Path("/proc/meminfo").is_file():
        values = {line.split(":")[0]: int(line.split()[1])
                  for line in Path("/proc/meminfo").read_text().splitlines()}
        if values["MemAvailable"] / values["MemTotal"] < .25:
            raise RuntimeError("Native MAIL requires at least25 percent memory headroom")


def sanitize(path):
    text = path.read_text(errors="replace")
    for secret in REDACT:
        text = text.replace(secret, "[synthetic secret redacted]")
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[bearer redacted]", text)
    path.write_text(text)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sources(root):
    files = [p for p in root.rglob("*") if p.is_file()
             and not any(part in ("target", ".git", "node_modules", "_worktrees", ".worktrees")
                         for part in p.relative_to(root).parts)
             and ("src" in p.relative_to(root).parts
                  or p.suffix in (".java", ".xml", ".properties", ".json", ".py", ".yml", ".yaml", ".tpl", ".sh")
                  or p.name in ("mvnw", "mvnw.cmd", "maven.config", "jvm.config", "Dockerfile"))]
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def run(command, cwd, environment, log, timeout=600):
    ensure_headroom()
    with log.open("wb") as output:
        result = subprocess.Popen(command, cwd=cwd, env=environment, stdout=output,
                                  stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while result.poll() is None:
                ensure_headroom()
                if time.monotonic() > deadline:
                    raise RuntimeError("Native MAIL command exceeded finite deadline")
                time.sleep(2)
        finally:
            if RESOURCE_SUPPORT is not None:
                cleanup = RESOURCE_SUPPORT.stop(result)
                with (EVIDENCE / "process-cleanup.jsonl").open("a") as receipt:
                    receipt.write(json.dumps(cleanup) + "\n")
            elif result.poll() is None:
                os.killpg(result.pid, signal.SIGTERM)
                try:
                    result.wait(10)
                except subprocess.TimeoutExpired:
                    os.killpg(result.pid, signal.SIGKILL)
                    result.wait()
    sanitize(log)
    if result.returncode:
        raise RuntimeError("Native MAIL command failed; inspect sanitized " + log.name)


def strict_report(user, started, evidence):
    reports = list((user / "target/surefire-reports").glob("TEST-*RealMailActorNativeGate.xml"))
    if len(reports) != 1 or reports[0].stat().st_mtime < started:
        raise RuntimeError("Fresh native MAIL execution XML is required")
    report = XML.parse(reports[0]).getroot()
    counts = {k: int(report.get(k, "0")) for k in ("tests", "failures", "errors", "skipped")}
    rows = list(report.iter("testcase"))
    if (counts != {"tests": 4, "failures": 0, "errors": 0, "skipped": 0}
            or len(rows) != 4 or {row.get("name") for row in rows} != EXPECTED_CASES
            or any(row.get("classname") != "de.caritas.cob.userservice.api.config.auth.RealMailActorNativeGate"
                   or any(row.find(marker) is not None for marker in ("failure", "error", "skipped"))
                   for row in rows)):
        raise RuntimeError("Native MAIL must execute all four real cases without skips")
    archive = evidence / "native-xml"
    archive.mkdir(exist_ok=False)
    shutil.copy2(reports[0], archive / reports[0].name)
    return counts


def remaining_actors(case, fixture_module):
    """Installer fixture only: create the other four actors beside canonical protected MAIL."""
    actors = []
    baseline = case.admin("GET", "/clients")
    native = [client for client in baseline if client["clientId"] == "admin-permissions"]
    if (len(native) != 1 or native[0].get("serviceAccountsEnabled") is not True
            or native[0].get("authorizationServicesEnabled") is not True
            or native[0].get("publicClient") is not False
            or native[0].get("fullScopeAllowed") is not False):
        raise RuntimeError("Measured native FGAP control client is required")
    if {client["clientId"] for client in baseline if client.get("serviceAccountsEnabled")} != {
            "admin-permissions", "backend-mail"}:
        raise RuntimeError("Unexpected service client existed before fixture actor creation")
    basics = case.admin("GET", "/client-scopes")
    basic = next(scope for scope in basics if scope["name"] == "basic")
    existing_roles = {role["name"] for role in case.admin("GET", "/roles")}
    for key, client_id, roles, audiences in fixture_module.ACTORS:
        if key == "MAIL":
            client = case.admin("GET", "/clients/" + case.client)
            client["protocolMappers"] = [m for m in client["protocolMappers"]
                                          if m["protocolMapper"] != "oidc-audience-mapper"]
            client["protocolMappers"] += [fixture_module.audience_mapper(a) for a in audiences]
            case.admin("PUT", "/clients/" + case.client, client)
            case.admin("PUT", "/clients/" + case.client + "/default-client-scopes/" + basic["id"])
            actors.append({"key": key, "clientId": client_id, "secret": case_module.SECRET,
                           "subject": case_module.SUBJECT, "roles": list(roles),
                           "audiences": list(audiences)})
            continue
        for role in roles:
            if role not in existing_roles:
                case.admin("POST", "/roles", {"name": role})
                existing_roles.add(role)
        secret = "synthetic-native-" + uuid.uuid4().hex
        REDACT.append(secret)
        representation = {"clientId": client_id, "secret": secret, "publicClient": False,
                          "serviceAccountsEnabled": True, "standardFlowEnabled": False,
                          "directAccessGrantsEnabled": False, "fullScopeAllowed": False,
                          "defaultClientScopes": ["basic"], "optionalClientScopes": [],
                          "protocolMappers": [{"name": "actor-realm-roles", "protocol": "openid-connect",
                              "protocolMapper": "oidc-usermodel-realm-role-mapper",
                              "config": {"claim.name": "realm_access.roles", "multivalued": "true",
                                  "jsonType.label": "String", "access.token.claim": "true",
                                  "id.token.claim": "false", "usermodel.realmRoleMapping.rolePrefix": ""}}]
                              + [fixture_module.audience_mapper(a) for a in audiences]}
        case.admin("POST", "/clients", representation)
        client = next(c for c in case.admin("GET", "/clients") if c["clientId"] == client_id)
        account = case.admin("GET", "/clients/" + client["id"] + "/service-account-user")
        defaults = case.admin("GET", "/users/" + account["id"] + "/role-mappings/realm")
        if defaults:
            case.admin("DELETE", "/users/" + account["id"] + "/role-mappings/realm", defaults)
        grants = [case.admin("GET", "/roles/" + role) for role in roles]
        case.admin("POST", "/users/" + account["id"] + "/role-mappings/realm", grants)
        case.admin("POST", "/clients/" + client["id"] + "/scope-mappings/realm", grants)
        case.admin("PUT", "/users/" + account["id"] + "/groups/" + case.technical_group)
        actors.append({"key": key, "clientId": client_id, "secret": secret, "subject": account["id"],
                       "roles": list(roles), "audiences": list(audiences)})
    actual = [c for c in case.admin("GET", "/clients") if c.get("serviceAccountsEnabled")]
    if ({c["clientId"] for c in actual} != {a["clientId"] for a in actors} | {"admin-permissions"}
            or len(actors) != 5):
        raise RuntimeError("Exactly five routine actors plus native FGAP control client required")
    return actors


def main():
    global case_module, EVIDENCE, STAGE, RESOURCE_SUPPORT, MEMORY_LEASE
    parser = argparse.ArgumentParser()
    for name in ("keycloak", "cts", "user", "java-home", "evidence"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--resource-support", type=Path)
    args = parser.parse_args()
    args.keycloak, args.cts, args.user = [p.resolve() for p in (args.keycloak, args.cts, args.user)]
    args.evidence = args.evidence.resolve()
    args.evidence.mkdir(parents=True, exist_ok=True)
    EVIDENCE = args.evidence
    if args.resource_support is not None:
        if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
            raise RuntimeError("Joined hosted MAIL proof requires a fresh Linux GitHub job")
        installer = load("mail_installer_resource_support", ROOT / "scripts/ci/scenario-c-installer-proof.py")
        RESOURCE_SUPPORT = installer.load_support(args.resource_support.resolve())
        MEMORY_LEASE = RESOURCE_SUPPORT.configure_memory(3152, args.evidence)
        MEMORY_LEASE.check(starting=True)
        deadline = time.monotonic() + 1500

        def watch_resources(signum, frame):
            try:
                ensure_headroom()
                if time.monotonic() >= deadline:
                    raise RuntimeError("Hosted MAIL total resource lease expired")
            except Exception:
                signal.setitimer(signal.ITIMER_REAL, 0)
                raise

        signal.signal(signal.SIGALRM, watch_resources)
        signal.setitimer(signal.ITIMER_REAL, 2, 2)
    for root in (args.keycloak, args.cts, args.user):
        if not root.is_dir() or not (root / ".git").is_file():
            raise RuntimeError("Explicit existing isolated Git worktree required")
    environment = {**os.environ, "JAVA_HOME": str(args.java_home.resolve()),
                   "MAVEN_OPTS": "-Xmx512m -XX:MaxMetaspaceSize=192m -XX:ReservedCodeCacheSize=64m "
                   "-XX:MaxDirectMemorySize=32m -Xss512k -XX:ActiveProcessorCount=2",
                   "PATH": str(args.java_home.resolve() / "bin") + os.pathsep + os.environ["PATH"]}
    before = {"Helm": sources(ROOT), "Keycloak": sources(args.keycloak),
              "CTS": sources(args.cts), "User": sources(args.user)}
    if args.resource_support is not None:
        before["Support"] = sources(args.resource_support.resolve())
    (args.evidence / "source-before.json").write_text(json.dumps(before, indent=2))
    module = args.keycloak / "keycloak-image/smtp-control-spi"
    STAGE = "smtp-package"
    run(["mvn", "-q", "-f", str(module / "pom.xml"), "package"], args.keycloak, environment,
        args.evidence / "smtp-package.log")
    jar = module / "target/smtp-control-spi-1.0-SNAPSHOT.jar"
    if not jar.is_file():
        raise RuntimeError("Native SMTP provider jar required")
    case_module = load("native_smtp", args.keycloak / "scripts/test-smtp-control-permissions.py")
    REDACT.extend([case_module.SECRET, case_module.PASSWORD, case_module.INSTALLER,
                   base64.b64encode(case_module.KEY).decode(), "synthetic-native-mail-encryption"])
    fixture_module = load("five_actor_fixture", ROOT / "tests/scenario_c_receiver_fixture.py")
    environment["SMTP_CONTROL_TEST_JAR"] = str(jar)
    old_jar = os.environ.get("SMTP_CONTROL_TEST_JAR")
    os.environ["SMTP_CONTROL_TEST_JAR"] = str(jar)
    case = None
    server = None
    mongo_name = None
    try:
        STAGE = "native-smtp-start"
        case_module.SmtpControlPermissions.setUpClass()
        case = case_module.SmtpControlPermissions("test_real_canonical_protected_mail_can_apply_and_read_status")
        case.setUp()
        STAGE = "remaining-native-actors"
        actors = remaining_actors(case, fixture_module)
        realm = case.admin("GET")
        mongo_name = "oriso-mail-mongo-" + case.run_id[:12]
        STAGE = "isolated-mongo-start"
        subprocess.run(["docker", "run", "-d", "--name", mongo_name,
            "--label", "oriso-smtp-run-id=" + case.run_id, "--network", case.network,
            "--memory=384m", "--memory-swap=384m", "--cpus=1", "--pids-limit=128",
            "-p", "127.0.0.1::27017", MONGO_IMAGE, "--wiredTigerCacheSizeGB", "0.25"],
            check=True, capture_output=True)
        mongo_port = subprocess.check_output(["docker", "port", mongo_name, "27017/tcp"], text=True).strip()
        environment["ORISO_NATIVE_MAIL_MONGO_URI"] = "mongodb://" + mongo_port + "/native_mail_" + case.run_id
        with tempfile.TemporaryDirectory(prefix="oriso-native-mail-") as private:
            private = Path(private)
            fixture = private / "fixture.json"
            fixture.write_text(json.dumps({"identityModel": "FIVE_ACTORS", "issuer": case.base
                + "/realms/" + case_module.REALM, "tasks": actors}))
            fixture.chmod(0o600)
            classpath = private / "classpath"
            STAGE = "cts-test-support-compile"
            run(["./mvnw", "-q", "test-compile", "dependency:build-classpath",
                 "-DincludeScope=test", "-Dmdep.outputFile=" + str(classpath)], args.cts, environment,
                args.evidence / "cts-test-support-compile.log")
            cp = os.pathsep.join([str(args.cts / "target/test-classes"), str(args.cts / "target/classes"),
                                  classpath.read_text().strip()])
            STAGE = "user-test-support-compile"
            run(["./mvnw", "-q", "test-compile"], args.user, environment,
                args.evidence / "user-test-support-compile.log")
            ready = private / "ready"
            STAGE = "actual-cts-start"
            with (args.evidence / "cts-runtime.log").open("wb") as output:
                server = subprocess.Popen([str(args.java_home.resolve() / "bin/java"),
                    "-Xmx384m", "-XX:MaxMetaspaceSize=128m", "-XX:ReservedCodeCacheSize=32m",
                    "-XX:MaxDirectMemorySize=16m", "-Xss512k", "-XX:ActiveProcessorCount=2", "-cp", cp,
                    "de.caritas.cob.consultingtypeservice.api.controller.ScenarioCMailNativeServer",
                    str(fixture), str(ready)], cwd=args.cts, env=environment, stdin=subprocess.PIPE,
                    stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 180
            while not ready.is_file():
                ensure_headroom()
                if server.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Real CTS native fixture readiness failed")
                time.sleep(.5)
            data = json.loads(fixture.read_text())
            data["mailTransportEnvironment"] = {
                "TASK_IDENTITY_MODEL": "FIVE_ACTORS", "POD_NAMESPACE": "fixture",
                "KEYCLOAK_URL": case.base, "KEYCLOAK_REALM": case_module.REALM,
                "TASK_IDENTITY_ISSUER": data["issuer"],
                "KEYCLOAK_REALM_INTERNAL_ID": realm["id"],
                "CONSULTING_TYPE_SERVICE_URL": ready.read_text(),
                "IDENTITY_MAIL_CLIENT_ID": "backend-mail", "IDENTITY_MAIL_CLIENT_SECRET": case_module.SECRET,
                "IDENTITY_MAIL_SERVICE_SUBJECT": case_module.SUBJECT,
                "ORISO_TASK_CONTEXT_KEY": base64.b64encode(case_module.KEY).decode()}
            fixture.write_text(json.dumps(data))
            environment.update({"ORISO_TASK_TOKEN_FIXTURE": str(fixture), "ORISO_NATIVE_PYTHON": sys.executable,
                "ORISO_MAIL_TRANSPORT_DRIVER": str(ROOT / "tests/scenario_c_mail_transport_driver.py")})
            started = time.time()
            STAGE = "actual-broker-cts-smtp-ack"
            native_environment = {**environment, "MAVEN_OPTS": "-Xmx256m -XX:MaxMetaspaceSize=96m "
                "-XX:ReservedCodeCacheSize=32m -XX:MaxDirectMemorySize=16m -Xss512k -XX:ActiveProcessorCount=2"}
            run(["./mvnw", "-q", "-DargLine=-Xmx256m -XX:MaxMetaspaceSize=96m -XX:ReservedCodeCacheSize=32m "
                 "-XX:MaxDirectMemorySize=16m -Xss512k -XX:ActiveProcessorCount=2",
                 "-Dtest=RealMailActorNativeGate", "surefire:test@unit-tests"], args.user, native_environment,
                args.evidence / "user-native-mail.log")
            counts = strict_report(args.user, started, args.evidence)
            server.stdin.close()
            STAGE = "durable-readback"
            if server.wait(45):
                raise RuntimeError("Real saved CTS exact acknowledgement readback failed")
            if RESOURCE_SUPPORT is not None:
                closed = RESOURCE_SUPPORT.stop(server)
                with (EVIDENCE / "process-cleanup.jsonl").open("a") as receipt:
                    receipt.write(json.dumps(closed) + "\n")
            server = None
            if case.admin("GET")["smtpServer"]["host"] != "smtp.fixture.internal":
                raise RuntimeError("Actual SMTP provider state readback failed")
            after = {"Helm": sources(ROOT), "Keycloak": sources(args.keycloak),
                     "CTS": sources(args.cts), "User": sources(args.user)}
            if args.resource_support is not None:
                after["Support"] = sources(args.resource_support.resolve())
            (args.evidence / "source-after.json").write_text(json.dumps(after, indent=2))
            if before != after:
                raise RuntimeError("Native MAIL source inputs changed during execution")
            (args.evidence / "result.json").write_text(json.dumps({"counts": counts,
                "nativeImage": case_module.IMAGE, "smtpJarSha256": hashlib.sha256(jar.read_bytes()).hexdigest(),
                "sameBearerBrokerCtsSmtpAck": True, "exactSavedRevision": 7, "sourceInputsUnchanged": True,
                "routineActorClients": sorted(a["clientId"] for a in actors),
                "nativeBuiltinClient": "admin-permissions"}, indent=2))
    finally:
        if RESOURCE_SUPPORT is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
        owned = None
        runtime = []
        if RESOURCE_SUPPORT is not None and hasattr(case_module.SmtpControlPermissions, "run_id"):
            native = case_module.SmtpControlPermissions
            owned = RESOURCE_SUPPORT.resources.OwnedDocker("oriso-smtp-run-id", native.run_id)
            owned.plan("network", native.network)
            for name in (native.name, mongo_name):
                if name is None:
                    continue
                owned.plan("container", name)
                current = owned.inspect("container", name)
                if current is not None:
                    if current.get("Config", {}).get("Labels", {}).get(owned.label_key) != native.run_id:
                        raise RuntimeError("Refuse foreign MAIL runtime readback")
                    runtime.append({"name": name, "oomKilled": current["State"]["OOMKilled"],
                                    "restartCount": current["RestartCount"], "imageId": current["Image"],
                                    "memoryBytes": current["HostConfig"]["Memory"],
                                    "swapBytes": current["HostConfig"]["MemorySwap"]})
        if server is not None:
            if server.stdin and not server.stdin.closed:
                server.stdin.close()
            try:
                server.wait(45)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            if RESOURCE_SUPPORT is not None:
                RESOURCE_SUPPORT.stop(server)
        if case is not None:
            case.doCleanups()
        if mongo_name:
            label = subprocess.run(["docker", "inspect", "--format",
                '{{index .Config.Labels "oriso-smtp-run-id"}}', mongo_name], capture_output=True, text=True)
            if label.returncode == 0:
                if label.stdout.strip() != case.run_id:
                    raise RuntimeError("Refuse Mongo cleanup: run label differs")
                subprocess.run(["docker", "rm", "-f", "-v", mongo_name], check=True, capture_output=True)
        case_module.SmtpControlPermissions.doClassCleanups()
        if owned is not None:
            cleanup = owned.cleanup()
            (args.evidence / "cleanup.json").write_text(json.dumps({"runtime": runtime, "cleanup": cleanup}, indent=2))
            if (len(runtime) != 2
                    or any(row["oomKilled"] is not False or row["restartCount"] != 0
                           or row["memoryBytes"] <= 0 or row["swapBytes"] != row["memoryBytes"] for row in runtime)
                    or len(cleanup) != 3 or any(row.get("confirmedAbsent") is not True for row in cleanup)):
                raise RuntimeError("Joined MAIL requires healthy owned runtimes and confirmed cleanup")
        for log in args.evidence.glob("*.log"):
            sanitize(log)
        if old_jar is None:
            os.environ.pop("SMTP_CONTROL_TEST_JAR", None)
        else:
            os.environ["SMTP_CONTROL_TEST_JAR"] = old_jar
    print("NATIVE_MAIL_4_CASES_PASSED")


if __name__ == "__main__":
    try:
        main()
    except Exception as failure:
        if RESOURCE_SUPPORT is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
        if EVIDENCE:
            for log in EVIDENCE.glob("*.log"):
                sanitize(log)
            diagnostic = {"stage": STAGE, "errorType": type(failure).__name__}
            diagnostic["locations"] = [{"file": Path(frame.filename).name,
                                        "line": frame.lineno, "function": frame.name}
                                       for frame in traceback.extract_tb(failure.__traceback__)]
            if isinstance(failure, subprocess.CalledProcessError):
                diagnostic["command"] = list(failure.cmd[:3])
                diagnostic["returncode"] = failure.returncode
                stderr = failure.stderr or b""
                if isinstance(stderr, bytes):
                    stderr = stderr.decode(errors="replace")
                for secret in REDACT:
                    stderr = stderr.replace(secret, "[synthetic secret redacted]")
                diagnostic["sanitizedStderr"] = stderr[:2048]
            elif type(failure) is RuntimeError:
                diagnostic["safeReason"] = str(failure)
            (EVIDENCE / "failure.json").write_text(json.dumps(diagnostic, indent=2))
        print("NATIVE_MAIL_GATE_FAILED: inspect sanitized local evidence", file=sys.stderr)
        sys.exit(2)
