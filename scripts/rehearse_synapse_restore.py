#!/usr/bin/env python3
"""Restore disposable Synapse/PostgreSQL fixtures from two encrypted streams."""

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from uuid import uuid4


SYNAPSE_IMAGE = "matrixdotorg/synapse@sha256:5f868df1f5772907c6dbe973a9b69ab530a5d6bb317c011a3788f7ad78eb1292"
POSTGRES_IMAGE = "postgres@sha256:23799e521dee9265a8b388ecf3bc0f1eaa34b95be67f5a76af4f8f6adb44c720"
SERVER = "restore-fixture.invalid"
SIGNING_KEY = "fixture.signing.key"
MESSAGE = "Synthetic Synapse recovery marker"
MEDIA = "Synthetic Synapse media marker\n"
DATABASE_ARCHIVE = "synapse-postgres.dump.age"
STATE_ARCHIVE = "synapse-media-signing.tar.age"
PLATFORM = "linux/amd64"
COMMAND_TIMEOUT = 35
TRANSFER_TIMEOUT = 90

# This program runs inside the isolated Synapse container. The password arrives on
# stdin and neither tokens nor private key material crosses the output boundary.
CLIENT_API = r"""
import hashlib
import json
import sys
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, ProxyHandler, build_opener

settings = json.load(sys.stdin)
opener = build_opener(ProxyHandler({}))
client = "/_matrix/client/v3"
username = "restore_fixture"

def request(path, method="GET", body=None, token=None):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if isinstance(body, bytes):
        headers["Content-Type"] = "application/octet-stream"
    elif body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(body).encode()
    try:
        with opener.open(Request("http://127.0.0.1:8008" + path, data=body,
                                 headers=headers, method=method), timeout=10) as response:
            return response.read()
    except HTTPError as exc:
        raise RuntimeError(f"{method} {path.split('?')[0]} returned HTTP {exc.code}") from None

def read_json(*args, **kwargs):
    return json.loads(request(*args, **kwargs))

try:
    if settings["mode"] == "seed":
        auth = read_json(client + "/register", "POST", {
            "username": username, "password": settings["password"],
            "auth": {"type": "m.login.dummy"}, "device_id": "SOURCE_FIXTURE"
        })
        token = auth["access_token"]
        room = read_json(client + "/createRoom", "POST",
                         {"preset": "private_chat", "name": "Restore Fixture"}, token)["room_id"]
        encoded_room = quote(room, safe="")
        event = read_json(client + f"/rooms/{encoded_room}/send/m.room.message/fixture-text",
                          "PUT", {"msgtype": "m.text", "body": settings["message"]}, token)["event_id"]
        uri = read_json("/_matrix/media/v3/upload?filename=fixture.txt", "POST",
                        settings["media"].encode(), token)["content_uri"]
        media_prefix = "mxc://" + settings["server"] + "/"
        if not uri.startswith(media_prefix):
            raise RuntimeError("Media upload produced an unexpected server name")
        attachment = read_json(
            client + f"/rooms/{encoded_room}/send/m.room.message/fixture-attachment",
            "PUT", {"msgtype": "m.file", "body": "fixture.txt", "url": uri}, token
        )["event_id"]
        fixture = {"room": room, "event": event, "attachment": attachment,
                   "media": uri.removeprefix(media_prefix)}
    else:
        auth = read_json(client + "/login", "POST", {
            "type": "m.login.password", "identifier": {"type": "m.id.user", "user": username},
            "password": settings["password"], "device_id": "RESTORE_FIXTURE"
        })
        token = auth["access_token"]
        fixture = settings["fixture"]

    room_path = client + "/rooms/" + quote(fixture["room"], safe="") + "/event/"
    message = read_json(room_path + quote(fixture["event"], safe=""), token=token)["content"]
    attachment = read_json(room_path + quote(fixture["attachment"], safe=""), token=token)["content"]
    media_path = "/_matrix/client/v1/media/download/" + settings["server"] + "/" + quote(fixture["media"], safe="")
    media = request(media_path, token=token)
    key = read_json("/_matrix/key/v2/server")
    if message != {"msgtype": "m.text", "body": settings["message"]}:
        raise RuntimeError("The original room message content did not match")
    if attachment.get("url") != "mxc://" + settings["server"] + "/" + fixture["media"]:
        raise RuntimeError("The attachment event did not identify the original media")
    if media != settings["media"].encode():
        raise RuntimeError("The original authenticated media bytes did not match")
    if key.get("server_name") != settings["server"] or not key.get("verify_keys"):
        raise RuntimeError("The signing endpoint did not return this server's verification key")
    public_keys = json.dumps(key["verify_keys"], sort_keys=True, separators=(",", ":")).encode()
    print(json.dumps({"fixture": fixture, "media_bytes": len(media),
                      "media_sha256": hashlib.sha256(media).hexdigest(),
                      "signing_public_key_sha256": hashlib.sha256(public_keys).hexdigest()}))
except (OSError, ValueError, KeyError, RuntimeError) as exc:
    print(json.dumps({"error": str(exc)}))
"""

# A bounded pipeline owns the lifetime of each child. This small middle stage
# measures the exact plaintext octets without leaving a plaintext archive.
MEASURE_STREAM = """
import hashlib, json, sys
count = 0
digest = hashlib.sha256()
for block in iter(lambda: sys.stdin.buffer.read(65536), b""):
    count += len(block)
    digest.update(block)
    sys.stdout.buffer.write(block)
sys.stdout.buffer.flush()
print(json.dumps({"bytes": count, "sha256": digest.hexdigest()}), file=sys.stderr)
"""


def run(args, label, *, payload=None, check=True, timeout=COMMAND_TIMEOUT):
    result = subprocess.run(args, input=payload, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{label} failed with exit status {result.returncode}.")
    return result


def docker_local():
    context = run(["docker", "context", "show"], "Find Docker context").stdout.strip()
    host = json.loads(run([
        "docker", "context", "inspect", context, "--format", "{{json .Endpoints.docker.Host}}",
    ], "Find Docker endpoint").stdout)
    if not isinstance(host, str) or not host.startswith("unix:///"):
        raise RuntimeError("Use a local Docker context with a Unix socket.")
    return ["env", "-u", "DOCKER_HOST", "-u", "DOCKER_CONTEXT", "docker", "--host", host]


def pipeline(producer, consumer, label, *, output=subprocess.DEVNULL):
    expiry = monotonic() + TRANSFER_TIMEOUT
    with ExitStack() as stack:
        processes = []
        def process(args, **kwargs):
            child = stack.enter_context(subprocess.Popen(args, **kwargs))
            processes.append(child)
            return child

        try:
            source = process(producer, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            meter = process(
                [sys.executable, "-c", MEASURE_STREAM], stdin=source.stdout,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            source.stdout.close()
            sink = process(consumer, stdin=meter.stdout, stdout=output, stderr=subprocess.DEVNULL)
            meter.stdout.close()
            for child in reversed(processes):
                child.wait(timeout=max(0, expiry - monotonic()))
            if any(child.returncode for child in processes):
                statuses = ", ".join(str(child.returncode) for child in processes)
                raise RuntimeError(f"{label} failed (producer/meter/consumer: {statuses}).")
            measure = json.load(meter.stderr)
            if measure["bytes"] <= 0:
                raise RuntimeError(f"{label} received an empty stream.")
            return measure
        finally:
            for child in reversed(processes):
                if child.poll() is None:
                    child.kill()


@contextmanager
def internal_network(docker):
    name = f"oriso-synapse-restore-{uuid4().hex}"
    try:
        run([*docker, "network", "create", "--internal", name], "Create internal fixture network")
        flag = run([
            *docker, "network", "inspect", "--format", "{{.Internal}}", name,
        ], "Verify fixture network isolation").stdout.strip()
        if flag != "true":
            raise RuntimeError("The fixture network is not internal.")
        yield name
    finally:
        run([*docker, "network", "rm", name], "Remove fixture network", check=False)


@contextmanager
def container(docker, network, image, options, command=()):
    name = network + "-" + ("postgres" if image == POSTGRES_IMAGE else "synapse")
    started = False
    try:
        run([
            *docker, "run", "--detach", "--pull", "never", "--platform", PLATFORM,
            "--network", network, "--name", name, *options, image, *command,
        ], "Start disposable container")
        started = True
        ports = json.loads(run([
            *docker, "inspect", "--format", "{{json .HostConfig.PortBindings}}", name,
        ], "Verify no host-published ports").stdout)
        if ports:
            raise RuntimeError("Fixture container has a host port binding.")
        yield name
    finally:
        removed = run([*docker, "rm", "--force", "--volumes", name], "Remove fixture container", check=False)
        if started and removed.returncode:
            raise RuntimeError("Could not remove the disposable container.")


def wait_ready(args, label):
    expiry = monotonic() + 75
    while (remaining := expiry - monotonic()) > 0:
        if run(args, label, check=False, timeout=remaining).returncode == 0:
            return
        sleep(0.3)
    raise RuntimeError(f"{label} did not become ready.")


@contextmanager
def database(docker, network, env_file):
    with container(docker, network, POSTGRES_IMAGE, [
        "--network-alias", "postgres", "--env-file", str(env_file),
        "--tmpfs", "/var/lib/postgresql",
    ]) as name:
        cli = [*docker, "exec", name]
        wait_ready([*cli, "pg_isready", "-q", "-h", "127.0.0.1", "-U", "fixture", "-d", "synapse"], "PostgreSQL")
        yield name, cli


def psql(cli, sql):
    return run([*cli, "psql", "-XAtq", "-v", "ON_ERROR_STOP=1", "-U", "fixture", "-d", "synapse",
                "-c", sql], "Read/write synthetic PostgreSQL fixture").stdout.strip()


def synapse_config(directory, db_password, secrets_config):
    directory.mkdir(mode=0o700)
    conf = {
        "server_name": SERVER, "report_stats": False, "pid_file": "/data/homeserver.pid",
        "signing_key_path": f"/data/{SIGNING_KEY}", "media_store_path": "/data/media_store",
        "trusted_key_servers": [], "federation_domain_whitelist": [],
        "enable_registration": True, "enable_registration_without_verification": True,
        "listeners": [{
            "port": 8008, "bind_addresses": ["127.0.0.1"], "type": "http", "tls": False,
            "resources": [{"names": ["client", "federation"], "compress": False}],
        }],
        "database": {"name": "psycopg2", "args": {
            "user": "fixture", "password": db_password, "database": "synapse", "host": "postgres",
        }},
        **secrets_config,
    }
    (directory / "homeserver.yaml").write_text(json.dumps(conf), encoding="utf-8")


@contextmanager
def synapse(docker, network, directory):
    with container(docker, network, SYNAPSE_IMAGE, [
        "--user", "0:0", "--mount", f"type=bind,source={directory},target=/data",
        "--entrypoint", "python",
    ], ["-m", "synapse.app.homeserver", "--config-path", "/data/homeserver.yaml"]) as name:
        wait_ready([
            *docker, "exec", name, "python", "-c",
            "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8008/_matrix/client/versions', timeout=2)",
        ], "Synapse")
        yield name


def matrix_fixture(docker, name, password, mode, fixture=None):
    payload = json.dumps({
        "mode": mode, "password": password, "fixture": fixture,
        "message": MESSAGE, "media": MEDIA, "server": SERVER,
    })
    result = run([*docker, "exec", "-i", name, "python", "-c", CLIENT_API],
                 "Exercise the synthetic Matrix API", payload=payload)
    values = json.loads(result.stdout)
    if "error" in values:
        raise RuntimeError(f"Matrix fixture check failed: {values['error']}")
    return values


def ciphertext_sha256(path):
    with path.open("rb") as data:
        return hashlib.file_digest(data, "sha256").hexdigest()


def seal(producer, archive, recipient):
    with archive.open("xb") as encrypted:
        measure = pipeline(producer, ["age", "--encrypt", "-r", recipient], archive.name, output=encrypted)
    return {
        "filename": archive.name, "ciphertext_sha256": ciphertext_sha256(archive),
        "ciphertext_bytes": archive.stat().st_size, "source": measure,
    }


def decrypt(archive, identity):
    return ["age", "--decrypt", "--identity", str(identity), str(archive)]


def verify_archive(archive, receipt, identity):
    if ciphertext_sha256(archive) != receipt["ciphertext_sha256"]:
        raise RuntimeError(f"Ciphertext checksum failed for {archive.name}.")
    checked = pipeline(decrypt(archive, identity), ["cat"], "Authenticate ciphertext")
    if checked != receipt["source"]:
        raise RuntimeError(f"Decrypted bytes differ from the source for {archive.name}.")


def restore_archive(archive, receipt, identity, consumer):
    result = pipeline(decrypt(archive, identity), consumer, "Restore " + archive.name)
    if result != receipt["source"]:
        raise RuntimeError(f"Restore input differs from the source for {archive.name}.")
    receipt["restore"] = result


def rehearse(docker, scratch, stage):
    started = datetime.now(timezone.utc)
    start = monotonic()
    db_password = secrets.token_hex(24)
    password = secrets.token_hex(24)
    secrets_config = {key: secrets.token_hex(32) for key in ("macaroon_secret_key", "form_secret")}
    env_file = scratch / "postgres.env"
    env_file.write_text(
        "POSTGRES_USER=fixture\nPOSTGRES_DB=synapse\nPGDATA=/var/lib/postgresql/fixture\n"
        "POSTGRES_INITDB_ARGS=--locale=C --encoding=UTF8\n"
        f"POSTGRES_PASSWORD={db_password}\n", encoding="utf-8",
    )
    identity = scratch / "age-fixture"
    run(["age-keygen", "-o", str(identity)], "Create ephemeral age identity")
    recipient = run(["age-keygen", "-y", str(identity)], "Read ephemeral age recipient").stdout.strip()
    source_dir = scratch / "source"
    synapse_config(source_dir, db_password, secrets_config)
    pg_archive = stage / DATABASE_ARCHIVE
    state_archive = stage / STATE_ARCHIVE

    with internal_network(docker) as network, database(docker, network, env_file) as (_, pg):
        with synapse(docker, network, source_dir) as server:
            baseline = matrix_fixture(docker, server, password, "seed")
            run([*docker, "stop", "--time", "15", server], "Stop source Synapse for a coherent backup")
        psql(pg, """
            INSERT INTO e2e_one_time_keys_json
                (user_id, device_id, algorithm, key_id, ts_added_ms, key_json)
            VALUES ('@restore_fixture:restore-fixture.invalid', 'SOURCE_FIXTURE',
                    'signed_curve25519', 'fixture-key', 1, '{}')
        """)
        if psql(pg, "SELECT count(*) FROM e2e_one_time_keys_json") != "1":
            raise RuntimeError("The source must contain exactly one dummy one-time key.")
        postgres_version = psql(pg, "SHOW server_version")
        db_receipt = seal([
            *pg, "pg_dump", "-U", "fixture", "--format=custom",
            "--exclude-table-data=e2e_one_time_keys_json", "synapse",
        ], pg_archive, recipient)
        state_receipt = seal([
            "tar", "-cf", "-", "-C", str(source_dir), "media_store", SIGNING_KEY,
        ], state_archive, recipient)
    shutil.rmtree(source_dir)
    if source_dir.exists():
        raise RuntimeError("The plaintext source directory was not removed.")

    for path, receipt in [(pg_archive, db_receipt), (state_archive, state_receipt)]:
        verify_archive(path, receipt, identity)
    restore_start = monotonic()
    restore_dir = scratch / "restore"
    synapse_config(restore_dir, db_password, secrets_config)
    with internal_network(docker) as network, database(docker, network, env_file) as (dbname, pg):
        if psql(pg, "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'") != "0":
            raise RuntimeError("The restore database is not empty.")
        restore_archive(pg_archive, db_receipt, identity, [
            *docker, "exec", "-i", dbname, "pg_restore", "-U", "fixture", "-d", "synapse",
            "--no-owner", "--exit-on-error", "--single-transaction",
        ])
        if psql(pg, "SELECT count(*) FROM e2e_one_time_keys_json") != "0":
            raise RuntimeError("A backed-up one-time key would reach the restored Synapse.")
        restore_archive(state_archive, state_receipt, identity,
                        ["tar", "-xf", "-", "-C", str(restore_dir)])
        with synapse(docker, network, restore_dir) as server:
            recovered = matrix_fixture(docker, server, password, "verify", baseline["fixture"])
            if recovered != baseline:
                raise RuntimeError("Restored event, media or server verification key does not match.")
            synapse_version = run([
                *docker, "exec", server, "python", "-c", "import synapse; print(synapse.__version__)",
            ], "Read Synapse version").stdout.strip()

    receipt = {
        "synthetic": True,
        "platform": PLATFORM,
        "images": {"synapse": SYNAPSE_IMAGE, "postgres": POSTGRES_IMAGE},
        "versions": {"synapse": synapse_version, "postgres": postgres_version},
        "archives": [db_receipt, state_receipt],
        "checks": {
            "private_network_no_published_ports": True, "plaintext_source_removed": True,
            "room_message_and_attachment_event_restored": True, "authenticated_media_restored": True,
            "source_one_time_keys": 1, "restored_one_time_keys_before_synapse": 0,
            "signing_public_key_unchanged": True,
        },
        "media_bytes": recovered["media_bytes"], "media_sha256": recovered["media_sha256"],
        "signing_public_key_sha256": recovered["signing_public_key_sha256"],
        "started_at": started.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(monotonic() - start, 3),
        "restore_seconds": round(monotonic() - restore_start, 3),
    }
    (stage / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


def interrupted(_signum, _frame):
    raise InterruptedError("Synapse rehearsal interrupted.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_dir", type=Path, help="New directory for ciphertext and receipt")
    result = parser.parse_args().result_dir.absolute()
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if result.exists() or result.is_symlink():
            raise FileExistsError(f"Result path already exists: {result}")
        docker = docker_local()
        with TemporaryDirectory(prefix=f".{result.name}-", dir=result.parent) as staging:
            stage = Path(staging) / "result"
            stage.mkdir(mode=0o700)
            with TemporaryDirectory(prefix="oriso-synapse-fixture-") as scratch:
                rehearse(docker, Path(scratch).resolve(), stage)
            if result.exists() or result.is_symlink():
                raise FileExistsError(f"Result path already exists: {result}")
            stage.rename(result)
    except subprocess.TimeoutExpired:
        print("Synapse rehearsal timed out.", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError, KeyboardInterrupt) as error:
        print(f"Synapse rehearsal failed: {error}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
