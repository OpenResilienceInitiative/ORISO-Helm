#!/usr/bin/env python3
"""Rehearse a ciphertext-only MongoDB restore with disposable synthetic data."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from uuid import uuid4


DATABASE = "consulting_types"
SENTINEL_DATABASE = "outside_scope"
ARCHIVE_NAME = f"{DATABASE}.archive.gz.age"
COMMAND_TIMEOUT = 30
TRANSFER_TIMEOUT = 90
MONGO_ADDRESS = ["--host", "127.0.0.1", "--port", "27017"]

SEED = """
const source = db.getSiblingDB("consulting_types");
source.records.insertMany([
  { _id: ObjectId("000000000000000000000001"), key: "alpha",
    observedAt: ISODate("2026-11-20T13:00:00Z"), quantity: NumberLong("9007199254740993") },
  { _id: ObjectId("000000000000000000000002"), key: "beta",
    amount: NumberDecimal("7.20"), enabled: true }
]);
source.records.createIndex({ key: 1 }, { name: "records_key_unique", unique: true });
db.getSiblingDB("outside_scope").sentinel.insertOne({ _id: "must-not-be-restored" });
"""

SNAPSHOT = """
const target = db.getSiblingDB("consulting_types");
const databases = db.adminCommand({ listDatabases: 1, nameOnly: true }).databases
  .map(item => item.name).filter(name => !["admin", "config", "local"].includes(name)).sort();
const collections = target.getCollectionNames().sort();
const records = target.records.find().sort({ _id: 1 }).toArray();
const indexes = target.records.getIndexes()
  .map(index => ({ name: index.name, key: index.key, unique: index.unique === true }))
  .sort((a, b) => a.name.localeCompare(b.name));
print(EJSON.stringify({ databases, collections, records, indexes }, { relaxed: false }));
"""


class TransferFailed(RuntimeError):
    def __init__(self, label, producer_code, consumer_code):
        super().__init__(
            f"{label} failed (producer exit {producer_code}, consumer exit {consumer_code})."
        )
        self.producer_code = producer_code


def command(args, *, check=True, timeout=COMMAND_TIMEOUT, stdout=subprocess.PIPE):
    return subprocess.run(
        args, check=check, timeout=timeout, stdin=subprocess.DEVNULL,
        stdout=stdout, stderr=subprocess.PIPE, text=True,
    )


def local_docker():
    context = command(["docker", "context", "show"]).stdout.strip()
    host = json.loads(command([
        "docker", "context", "inspect", context, "--format",
        "{{json .Endpoints.docker.Host}}",
    ]).stdout)
    if not isinstance(host, str) or not host.startswith("unix:///"):
        raise RuntimeError("A local Docker context using a Unix socket is required.")
    return ["env", "-u", "DOCKER_CONTEXT", "-u", "DOCKER_HOST", "docker", "--host", host]


def stop(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def transfer(source, destination, label, *, output=subprocess.DEVNULL):
    deadline = monotonic() + TRANSFER_TIMEOUT
    with subprocess.Popen(source, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as producer:
        try:
            with subprocess.Popen(
                destination, stdin=producer.stdout, stdout=output, stderr=subprocess.DEVNULL,
            ) as consumer:
                try:
                    producer.stdout.close()
                    consumer_code = consumer.wait(timeout=max(0, deadline - monotonic()))
                    producer_code = producer.wait(timeout=max(0, deadline - monotonic()))
                finally:
                    stop(consumer)
        finally:
            stop(producer)
    if producer_code or consumer_code:
        raise TransferFailed(label, producer_code, consumer_code)


def mongo_shell(client, script, **options):
    return command([*client, "mongosh", "--quiet", "--norc", *MONGO_ADDRESS, "--eval", script], **options)


def await_mongo(client, predicate, label):
    deadline = monotonic() + COMMAND_TIMEOUT
    while (remaining := deadline - monotonic()) > 0:
        probe = mongo_shell(
            client, f"if (!({predicate})) quit(1)", check=False, timeout=remaining,
        )
        if probe.returncode == 0:
            return
        sleep(0.25)
    raise RuntimeError(f"MongoDB fixture did not become {label}.")


@contextmanager
def mongo_fixture(docker, image, platform):
    name = f"oriso-mongo-rehearsal-{uuid4().hex}"
    client = [*docker, "exec", name]
    started = False
    try:
        command([
            *docker, "run", "--detach", "--name", name, "--pull", "never",
            "--platform", platform, "--network", "none", "--tmpfs", "/data/db",
            "--tmpfs", "/data/configdb", image, "mongod", "--bind_ip", "127.0.0.1",
            "--replSet", "rs0",
        ])
        started = True
        await_mongo(client, "db.adminCommand({ ping: 1 }).ok === 1", "reachable")
        mongo_shell(client, """
          const outcome = rs.initiate({
            _id: "rs0", members: [{ _id: 0, host: "127.0.0.1:27017" }]
          });
          if (outcome.ok !== 1) quit(1);
        """)
        await_mongo(client, "db.hello().isWritablePrimary", "writable")
        yield name, client
    finally:
        removal = command([*docker, "rm", "--force", "--volumes", name], check=False)
        if started and removal.returncode:
            raise RuntimeError("Could not remove the disposable MongoDB fixture.")


@contextmanager
def staged_result(destination):
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Result path already exists: {destination}")
    with TemporaryDirectory(prefix=f".{destination.name}-", dir=destination.parent) as scratch:
        stage = Path(scratch) / "result"
        stage.mkdir(mode=0o700)
        yield stage
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"Result path already exists: {destination}")
        stage.rename(destination)


def encrypted_dump(client, recipient, archive, *, extra=()):
    dump = [
        *client, "mongodump", *MONGO_ADDRESS, "--db", DATABASE, "--archive", "--gzip", *extra,
    ]
    with archive.open("xb") as output:
        transfer(dump, ["age", "--encrypt", "--recipient", recipient], "MongoDB dump", output=output)
    if not archive.stat().st_size:
        raise RuntimeError("The MongoDB dump produced no ciphertext.")


def verify_failed_dump(client, recipient, scratch):
    destination = scratch / "failed-dump"
    try:
        with staged_result(destination) as stage:
            encrypted_dump(client, recipient, stage / ARCHIVE_NAME, extra=("--invalid-fixture-option",))
    except TransferFailed as error:
        if error.producer_code and not destination.exists():
            return
    raise RuntimeError("A failed dump was not rejected before publication.")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_checksum(archive, expected):
    if sha256(archive) != expected:
        raise ValueError("Ciphertext SHA-256 does not match the receipt.")


def verify_damaged_archive(archive, expected, scratch):
    damaged = scratch / "damaged.age"
    shutil.copyfile(archive, damaged)
    with damaged.open("r+b") as handle:
        handle.seek(-1, os.SEEK_END)
        byte = handle.read(1)[0]
        handle.seek(-1, os.SEEK_END)
        handle.write(bytes([byte ^ 1]))
    try:
        verify_checksum(damaged, expected)
    except ValueError:
        return
    raise RuntimeError("The damaged archive passed checksum verification.")


def verify_identity(archive, identity, wrong_identity):
    decrypt = ["age", "--decrypt", "--identity"]
    if command([*decrypt, str(wrong_identity), str(archive)], check=False, stdout=subprocess.DEVNULL).returncode == 0:
        raise RuntimeError("The wrong age identity decrypted the archive.")
    command([*decrypt, str(identity), str(archive)], stdout=subprocess.DEVNULL)


def image_metadata(docker, name, platform):
    image_id = command([*docker, "inspect", "--format", "{{.Image}}", name]).stdout.strip()
    # A multi-platform reference has no architecture until an image variant is selected.
    details = command([
        *docker, "image", "inspect", "--platform", platform,
        "--format", "{{.Id}} {{.Os}}/{{.Architecture}}", image_id,
    ]).stdout.split()
    selected_id, selected_platform = details
    return {"id": selected_id, "platform": selected_platform}


def rehearse(destination, image, platform):
    docker = local_docker()
    started_at = datetime.now(timezone.utc)
    start = monotonic()
    with staged_result(destination) as stage, TemporaryDirectory(prefix="oriso-mongo-fixture-") as scratch:
        scratch = Path(scratch)
        identity = scratch / "fixture-identity"
        wrong_identity = scratch / "wrong-fixture-identity"
        for key in (identity, wrong_identity):
            command(["age-keygen", "--output", str(key)])
        recipient = command(["age-keygen", "-y", str(identity)]).stdout.strip()
        archive = stage / ARCHIVE_NAME

        with mongo_fixture(docker, image, platform) as (name, source):
            metadata = image_metadata(docker, name, platform)
            if metadata["platform"] != platform:
                raise RuntimeError("The fixture image does not match the requested platform.")
            mongo_shell(source, SEED)
            expected = json.loads(mongo_shell(source, SNAPSHOT).stdout)
            if expected["databases"] != [DATABASE, SENTINEL_DATABASE] or len(expected["records"]) != 2:
                raise RuntimeError("The expected synthetic source documents and sentinel are missing.")
            if not any(index["name"] == "records_key_unique" and index["unique"] for index in expected["indexes"]):
                raise RuntimeError("The expected synthetic source index is missing.")
            server_version = mongo_shell(source, "print(db.version())").stdout.strip()
            tools_version = command([*source, "mongodump", "--version"]).stdout.splitlines()[0]
            verify_failed_dump(source, recipient, scratch)
            encrypted_dump(source, recipient, archive)

        checksum = sha256(archive)
        verify_damaged_archive(archive, checksum, scratch)
        verify_checksum(archive, checksum)
        verify_identity(archive, identity, wrong_identity)
        expected["databases"] = [DATABASE]
        restore_start = monotonic()
        with mongo_fixture(docker, image, platform) as (name, restore):
            if image_metadata(docker, name, platform) != metadata:
                raise RuntimeError("Source and restore fixtures ran different images.")
            transfer(
                ["age", "--decrypt", "--identity", str(identity), str(archive)],
                [*docker, "exec", "-i", name, "mongorestore", *MONGO_ADDRESS,
                 "--archive", "--gzip", "--stopOnError"],
                "MongoDB restore",
            )
            actual = json.loads(mongo_shell(restore, SNAPSHOT).stdout)
            if actual != expected:
                raise RuntimeError("Restored BSON, indexes or database scope differ from the fixture.")

        receipt = {
            "synthetic": True,
            "archive": {"name": ARCHIVE_NAME, "sha256": checksum, "bytes": archive.stat().st_size},
            "mongodb": {"requested_image": image, **metadata, "version": server_version, "dump_tools": tools_version},
            "database": DATABASE,
            "collections": actual["collections"],
            "record_count": len(actual["records"]),
            "indexes": [index["name"] for index in actual["indexes"]],
            "checks": {
                "failed_dump_rejected": True,
                "damaged_ciphertext_rejected": True,
                "wrong_identity_rejected": True,
                "isolated_restore_matches": True,
                "unrelated_database_absent": True,
            },
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(monotonic() - start, 3),
            "restore_seconds": round(monotonic() - restore_start, 3),
        }
        with (stage / "receipt.json").open("x", encoding="utf-8") as report:
            json.dump(receipt, report, indent=2)
            report.write("\n")


def interrupted(_signum, _frame):
    raise InterruptedError("MongoDB rehearsal interrupted.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_dir", type=Path, help="New local directory for the ciphertext and receipt")
    parser.add_argument("--image", default="mongo:5.0", help="Already available local MongoDB image")
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"), default="linux/amd64")
    args = parser.parse_args()
    if args.image.startswith("-"):
        parser.error("--image must be a local image reference")
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        rehearse(args.result_dir.absolute(), args.image, args.platform)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print(f"MongoDB rehearsal failed: {error}", file=sys.stderr)
        return 1
    print(args.result_dir.absolute())
    return 0


if __name__ == "__main__":
    sys.exit(main())
