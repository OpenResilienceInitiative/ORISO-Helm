#!/usr/bin/env python3
"""Stage and publish one encrypted logical backup of the Dev MongoDB or Synapse PostgreSQL pod.

Same publication rules as backup_mariadb.py: the dump is streamed through gzip and age
(public recipients only), written into a private staging folder and published by one rename.
A failed run leaves no folder behind. The private age identity never reaches this host.
"""

import argparse
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory


def load_mariadb_writer():
    path = Path(__file__).resolve().with_name("backup_mariadb.py")
    spec = importlib.util.spec_from_file_location("oriso_backup_mariadb", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MARIADB = load_mariadb_writer()

# One entry per datastore. "pod" must match the rendered StatefulSet; tests keep them aligned.
TARGETS = {
    "mongodb": {
        "pod": "pod/mongodb-0",
        "container": "mongodb",
        "prefix": "mongodb",
        "artifact": "mongodb.archive.gz.age",
        # mongodump has no environment variable for the password, and a --password argument would show in the
        # process list. Its --config file may hold the password, so write a private file and remove it on exit.
        "dump": (
            ': "${MONGO_INITDB_ROOT_PASSWORD:?MongoDB credential is unavailable}"; '
            "umask 077; config=$(mktemp); trap 'rm -f \"$config\"' EXIT; "
            "printf \"password: '%s'\\n\" \"$(printf %s \"$MONGO_INITDB_ROOT_PASSWORD\" | sed \"s/'/''/g\")\" > \"$config\"; "
            'mongodump --archive --quiet --authenticationDatabase=admin '
            '--username="$MONGO_INITDB_ROOT_USERNAME" --config="$config"'
        ),
    },
    "matrix-postgres": {
        "pod": "pod/matrix-postgres-0",
        "container": "postgres",
        "prefix": "matrix-postgres",
        "artifact": "synapse.sql.gz.age",
        "dump": (
            ': "${POSTGRES_PASSWORD:?PostgreSQL credential is unavailable}"; '
            'export PGPASSWORD="$POSTGRES_PASSWORD"; '
            'exec pg_dump --host=localhost --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" '
            "--no-owner --clean --if-exists --exclude-table-data=public.e2e_one_time_keys_json"
        ),
    },
}


def check_target(kubernetes, target):
    subprocess.run(
        kubernetes + ["wait", "--for=condition=Ready", target["pod"], "--timeout=30s"],
        check=True, timeout=MARIADB.CLIENT_TIMEOUT_SECONDS,
    )
    fields = subprocess.check_output(
        kubernetes + ["get", target["pod"], "-o", MARIADB.POD_READINESS],
        text=True, timeout=MARIADB.CLIENT_TIMEOUT_SECONDS,
    ).split()
    if len(fields) != 2 or fields[1] != "True":
        raise RuntimeError(f"Could not confirm the identity of the ready {target['pod']}")
    return fields[0]


def backup(kubernetes, target, output_root, dump_timeout, age_command):
    check_target(kubernetes, target)
    command = kubernetes + [
        "exec", target["pod"], "--container", target["container"], "--", "sh", "-ec", target["dump"],
    ]
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output_root.is_symlink() or output_root.stat().st_mode & 0o077:
        raise RuntimeError("Use a private backup directory with no group or world permissions")
    started = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with TemporaryDirectory(prefix=f".{target['prefix']}-{started}-", dir=output_root) as scratch:
        stage = Path(scratch)
        MARIADB.save_dump(command, stage / target["artifact"], dump_timeout, age_command)
        destination = output_root / stage.name.removeprefix(".")
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("Backup destination already exists")
        stage.rename(destination)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kubeconfig", type=Path)
    parser.add_argument("context")
    parser.add_argument("namespace")
    parser.add_argument("datastore", choices=sorted(TARGETS))
    parser.add_argument("output_root", type=Path, nargs="?", help="private local directory for backups")
    parser.add_argument(
        "--check-target", action="store_true", help="report the ready pod without reading any data",
    )
    parser.add_argument(
        "--dump-timeout-seconds", type=int, default=MARIADB.DEFAULT_DUMP_TIMEOUT_SECONDS,
        help="time limit for the dump in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--age-recipients-file", type=Path,
        help="native age X25519 public recipients; required for a backup",
    )
    args = parser.parse_args()
    if not args.context.strip() or not args.namespace.strip():
        parser.error("context and namespace must be non-blank")
    if not args.kubeconfig.is_file():
        parser.error("kubeconfig must be an existing file")
    if not args.check_target and (args.output_root is None or args.age_recipients_file is None):
        parser.error("output_root and --age-recipients-file are required for a backup")
    if args.dump_timeout_seconds <= 0:
        parser.error("--dump-timeout-seconds must be positive")
    target = TARGETS[args.datastore]
    kubernetes = [
        "kubectl", "--kubeconfig", str(args.kubeconfig), "--context", args.context,
        "--namespace", args.namespace,
    ]
    os.umask(0o077)
    signal.signal(signal.SIGTERM, MARIADB.cancel_backup)
    signal.signal(signal.SIGINT, MARIADB.cancel_backup)
    operation = "Target check" if args.check_target else "Backup"
    try:
        if args.check_target:
            uid = check_target(kubernetes, target)
            checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            print(f"{checked_at} {args.datastore} target ready context={args.context} "
                  f"namespace={args.namespace} {target['pod']} uid={uid}")
            return 0
        age_command = MARIADB.read_age_command(args.age_recipients_file)
        destination = backup(kubernetes, target, args.output_root, args.dump_timeout_seconds, age_command)
    except subprocess.CalledProcessError as error:
        print(f"{operation} stopped after kubectl exited with status {error.returncode}", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired as error:
        print(f"{operation} stopped: kubectl timed out after {error.timeout} seconds", file=sys.stderr)
        return 1
    except (OSError, RuntimeError) as error:
        print(f"{operation} stopped: {error}", file=sys.stderr)
        return 1
    print(f"Completed local {args.datastore} backup: {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
