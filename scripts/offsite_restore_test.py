#!/usr/bin/env python3
"""Monthly restore test: fetch the newest off-site run of each datastore and restore it into throwaway containers.

Run it on a machine of a key holder (never on the Dev master): it needs an age identity and Docker.
The scratch containers have no network and no application access. Decrypted data exists only inside the
containers and in the pipes between age, gzip and the restore client; scratch files are removed at the end.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

IMAGES = {  # versions that run on Dev
    "mariadb": "mariadb:11.0.6",
    "mongodb": "mongo:5.0",
    "matrix-postgres": "postgres:15-alpine",
}
SYSTEM_SCHEMAS = "'information_schema','mysql','performance_schema','sys'"


def load_offsite():
    path = Path(__file__).resolve().with_name("offsite_backup.py")
    spec = importlib.util.spec_from_file_location("oriso_offsite_backup", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OFFSITE = load_offsite()


def docker(*args, stdin=None, check=True, timeout=600):
    result = subprocess.run(["docker", *args], stdin=stdin, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip()[-300:]}")
    return result


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def decrypted_stream(path, identity):
    """Yield a pipe carrying the gunzipped plaintext of one .age file; fail loudly on any bad stage."""
    age = subprocess.Popen(["age", "--decrypt", "-i", str(identity), str(path)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    gunzip = subprocess.Popen(["gzip", "-dc"], stdin=age.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    age.stdout.close()
    return age, gunzip


def pipe_into(path, identity, container, command, environment=()):
    age, gunzip = decrypted_stream(path, identity)
    exec_args = ["docker", "exec", "-i", *[a for e in environment for a in ("-e", e)], container, *command]
    restore = subprocess.run(exec_args, stdin=gunzip.stdout, capture_output=True, text=True, timeout=3600)
    gunzip.stdout.close()
    gz_err, age_err = gunzip.communicate()[1], age.communicate()[1]
    if age.returncode or gunzip.returncode:
        raise RuntimeError(f"{path.name}: authentication or gzip check failed (age={age.returncode}, gzip={gunzip.returncode})")
    if restore.returncode:
        raise RuntimeError(f"{path.name}: restore client failed: {restore.stderr.strip()[-300:]}")


def wait_until(container, command, seconds=120):
    for _ in range(seconds):
        if docker("exec", container, *command, check=False).returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"{container} did not become ready")


def start(name, image, env):
    args = ["run", "-d", "--rm", "--network", "none", "--name", name]
    for item in env:
        args += ["-e", item]
    docker(*args, image)


def restore_mariadb(files, identity, marker, name):
    password = secrets.token_hex(12)
    start(name, IMAGES["mariadb"], [f"MARIADB_ROOT_PASSWORD={password}"])
    # The image first runs a socket-only init server; only the final server listens on TCP.
    wait_until(name, ["mariadb-admin", "--protocol=tcp", "--host=127.0.0.1", "--user=root", f"--password={password}", "ping"])
    for path in files:
        pipe_into(path, identity, name, ["mariadb", "--user=root"], [f"MYSQL_PWD={password}"])
    query = (f"SELECT table_schema, COUNT(*) FROM information_schema.tables "
             f"WHERE table_schema NOT IN ({SYSTEM_SCHEMAS}) GROUP BY table_schema")
    out = docker("exec", "-e", f"MYSQL_PWD={password}", name, "mariadb", "--user=root", "-N", "-B", "-e", query).stdout
    tables = {line.split("\t")[0]: int(line.split("\t")[1]) for line in out.splitlines() if line}
    expected = {re.sub(r"\.sql\.gz\.age$", "", p.name) for p in files}
    missing = sorted(s for s in expected if tables.get(s, 0) == 0)
    if missing:
        raise RuntimeError(f"schemas without restored tables: {', '.join(missing)}")
    marker_rows = None
    if marker:
        schema, table = marker.split(".")
        marker_rows = int(docker("exec", "-e", f"MYSQL_PWD={password}", name, "mariadb", "--user=root", "-N", "-B",
                                 "-e", f"SELECT COUNT(*) FROM `{schema}`.`{table}`").stdout.strip())
    return {"tablesPerSchema": tables, "markerRows": marker_rows}


def restore_mongodb(files, identity, marker, name):
    start(name, IMAGES["mongodb"], [])
    wait_until(name, ["mongosh", "--quiet", "--eval", "db.runCommand({ping:1}).ok"])
    for path in files:
        pipe_into(path, identity, name, ["mongorestore", "--archive", "--drop", "--quiet"])
    script = ("const out={};for(const d of db.getMongo().getDBNames()){if(['admin','local','config'].includes(d))continue;"
              "const s=db.getSiblingDB(d);for(const c of s.getCollectionNames()){out[d+'.'+c]=s.getCollection(c).countDocuments({})}}"
              "print(JSON.stringify(out))")
    counts = json.loads(docker("exec", name, "mongosh", "--quiet", "--eval", script).stdout.strip().splitlines()[-1])
    if not counts:
        raise RuntimeError("no collections were restored")
    marker_rows = counts.get(marker) if marker else None
    if marker and not marker_rows:
        raise RuntimeError(f"marker collection {marker} is missing or empty")
    return {"collections": len(counts), "documents": sum(counts.values()), "markerRows": marker_rows}


def restore_postgres(files, identity, marker, name):
    password = secrets.token_hex(12)
    start(name, IMAGES["matrix-postgres"], [f"POSTGRES_PASSWORD={password}"])
    wait_until(name, ["pg_isready", "-U", "postgres"])
    time.sleep(3)  # the image restarts once after its init scripts
    wait_until(name, ["pg_isready", "-U", "postgres"])
    psql = ["psql", "-X", "-U", "postgres", "-v", "ON_ERROR_STOP=1"]
    for path in files:
        pipe_into(path, identity, name, [*psql, "-q"])

    def query(sql):
        return docker("exec", name, *psql, "-qAt", "-c", sql).stdout.strip()

    tables = int(query("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"))
    if not tables:
        raise RuntimeError("no tables were restored")
    evidence = {"publicTables": tables, "markerRows": None}
    if query("SELECT to_regclass('public.e2e_one_time_keys_json') IS NOT NULL") == "t":
        # Older archives may still contain stale Synapse one-time keys.
        one_time_keys = int(query(
            "TRUNCATE TABLE public.e2e_one_time_keys_json; "
            "SELECT COUNT(*) FROM public.e2e_one_time_keys_json"
        ))
        if one_time_keys != 0:
            raise RuntimeError("restored Synapse one-time keys were not cleared")
        evidence["oneTimeKeyRows"] = one_time_keys
    if marker:
        evidence["markerRows"] = int(query(f"SELECT count(*) FROM {marker}"))
    return evidence


RESTORERS = {"mariadb": restore_mariadb, "mongodb": restore_mongodb, "matrix-postgres": restore_postgres}


def read_receipt(path, expected_names):
    checksums = {}
    with path.open(encoding="utf-8") as receipt:
        for line in receipt:
            digest, separator, name = line.removesuffix("\n").partition("  ")
            if not separator or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                raise RuntimeError("invalid checksum entry in SHA256SUMS")
            if name in checksums:
                raise RuntimeError("duplicate archive entry in SHA256SUMS")
            if name not in expected_names:
                raise RuntimeError("receipt and downloaded archives do not match")
            checksums[name] = digest.lower()
    if checksums.keys() != expected_names:
        raise RuntimeError("receipt and downloaded archives do not match")
    return checksums


def fetch_newest(sftp, base, prefix, target):
    entries = sftp.names(f"{base}/{prefix}") or []
    names = [(n, w) for n in entries if (w := OFFSITE.run_stamp(n, prefix=prefix))]
    if not names:
        raise RuntimeError("no off-site run found")
    run = max(names, key=lambda r: r[1])[0]
    remote = f"{base}/{prefix}/{run}"
    sizes = sftp.sizes(remote)
    if OFFSITE.RECEIPT not in sizes:
        raise RuntimeError("backup run is missing SHA256SUMS")
    listed_names = set(sizes) - {OFFSITE.RECEIPT}
    archives = OFFSITE.archive_names(prefix, listed_names)

    target.mkdir(mode=0o700)
    receipt_path = target / OFFSITE.RECEIPT
    sftp.batch([f'get {remote}/{OFFSITE.RECEIPT} "{receipt_path}"'])
    checksums = read_receipt(receipt_path, listed_names)

    sftp.batch([f'get {remote}/{name} "{target / name}"' for name in sorted(listed_names)])
    for name, digest in checksums.items():
        if sha256(target / name) != digest:
            raise RuntimeError(f"checksum mismatch for {name}")
    return run, [target / name for name in sorted(archives)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="off-site configuration with read access to the box")
    parser.add_argument("--age-identity-file", type=Path, required=True)
    parser.add_argument("--datastores", nargs="+", choices=sorted(IMAGES), default=sorted(IMAGES))
    parser.add_argument("--marker", action="append", default=[], metavar="DATASTORE=NAME",
                        help="require a synthetic marker: mariadb=schema.table, mongodb=db.collection, "
                             "matrix-postgres=table (for rehearsals with synthetic data)")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    markers = dict(item.split("=", 1) for item in args.marker)
    if not shutil.which("age") or not shutil.which("docker"):
        print("age and docker are required", file=sys.stderr)
        return 2
    config = OFFSITE.load_config(args.config)
    base = config["storageBox"]["remoteBase"].rstrip("/")
    sftp = OFFSITE.Sftp(config["storageBox"])
    started, results = datetime.now(timezone.utc), {}
    scratch = Path(tempfile.mkdtemp(prefix="oriso-restore-test-"))
    scratch.chmod(0o700)
    try:
        for datastore in args.datastores:
            begin, container = time.monotonic(), f"oriso-restore-{datastore}-{secrets.token_hex(3)}"
            entry = results[datastore] = {"image": IMAGES[datastore]}
            try:
                run, files = fetch_newest(sftp, base, datastore, scratch / datastore)
                entry.update(run=run, archives={p.name: p.stat().st_size for p in files})
                entry.update(RESTORERS[datastore](files, args.age_identity_file, markers.get(datastore), container))
                entry["result"] = "PASS"
            except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
                entry.update(result="FAIL", error=str(error))
            finally:
                docker("rm", "-f", container, check=False)
                entry["seconds"] = round(time.monotonic() - begin, 1)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    passed = all(r["result"] == "PASS" for r in results.values())
    lines = [f"# Off-site restore test {started:%Y-%m-%d}", "",
             f"Result: **{'PASS' if passed else 'FAIL'}**  (started {started.isoformat(timespec='seconds')})", "",
             "| Datastore | Run | Result | Seconds | Detail |", "| --- | --- | --- | --- | --- |"]
    for name, r in results.items():
        detail = r.get("error") or ", ".join(f"{k}={v}" for k, v in r.items()
                                              if k not in {"image", "run", "result", "seconds", "archives", "error"})
        lines.append(f"| {name} ({r['image']}) | {r.get('run', '-')} | {r['result']} | {r['seconds']} | {detail} |")
    text = "\n".join(lines) + "\n"
    print(text)
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
