#!/usr/bin/env python3
"""Stage and publish one local logical backup of the Helm MariaDB StatefulSet."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import zlib


SYSTEM_SCHEMAS = {"information_schema", "mysql", "performance_schema", "sys"}
POD = "pod/mariadb-0"
CLIENT_TIMEOUT_SECONDS = 60
DEFAULT_DUMP_TIMEOUT_SECONDS = 3600
AGE_PUBLIC_RECIPIENT = re.compile(r"age1[023456789acdefghjklmnpqrstuvwxyz]{58}")
POD_READINESS = 'jsonpath={.metadata.uid} {.status.conditions[?(@.type=="Ready")].status}'
AUTH = (
    ': "${MYSQL_ROOT_PASSWORD:?MariaDB credential is unavailable}"; '
    'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; '
)
DISCOVER = AUTH + 'exec mariadb --user=root --batch --skip-column-names --execute "SHOW DATABASES"'
DUMP = (
    AUTH + "exec mariadb-dump --user=root --single-transaction --routines "
    '--triggers --events --databases "$1"'
)


@contextmanager
def managed_process(command, **options):
    with subprocess.Popen(command, **options) as process:
        try:
            yield process
        finally:
            if process.poll() is None:
                process.kill()


def wait_for_pipe(selector, deadline, command, timeout):
    remaining = deadline - monotonic()
    if remaining <= 0 or not selector.select(remaining):
        raise subprocess.TimeoutExpired(command, timeout)


def stream_dump(command, filename, write, deadline, timeout):
    with managed_process(command, stdout=subprocess.PIPE) as dump, selectors.DefaultSelector() as selector:
        wrote_data = False
        selector.register(dump.stdout, selectors.EVENT_READ)
        while True:
            wait_for_pipe(selector, deadline, command, timeout)
            block = dump.stdout.read1(64 * 1024)
            if not block:
                break
            write(block)
            wrote_data = True
        code = dump.wait(timeout=max(0, deadline - monotonic()))
        if code:
            raise RuntimeError(f"MariaDB dump for {filename} failed with exit status {code}")
        if not wrote_data:
            raise RuntimeError(f"MariaDB dump for {filename} was empty")


def read_age_command(recipients_file):
    try:
        recipients = [
            line for raw_line in recipients_file.read_text(encoding="utf-8").splitlines()
            if (line := raw_line.strip()) and not line.startswith("#")
        ] if recipients_file.is_file() else []
    except (OSError, UnicodeError) as error:
        raise RuntimeError("Could not read the age recipients file") from error
    if not recipients or any(not AGE_PUBLIC_RECIPIENT.fullmatch(line) for line in recipients):
        raise RuntimeError("The age recipients file must contain native age X25519 public recipients")
    executable = shutil.which("age")
    if not executable:
        raise RuntimeError("The age executable is required for encrypted MariaDB backups")
    command = [executable, "--encrypt"]
    for recipient in recipients:
        command.extend(["--recipient", recipient])
    return command


def save_encrypted_dump(command, path, age_command, deadline, timeout):
    try:
        with path.open("xb") as ciphertext, managed_process(
            age_command, stdin=subprocess.PIPE, stdout=ciphertext,
            stderr=subprocess.DEVNULL, bufsize=0,
        ) as age, selectors.DefaultSelector() as selector:
            os.set_blocking(age.stdin.fileno(), False)
            selector.register(age.stdin, selectors.EVENT_WRITE)
            compressor = zlib.compressobj(wbits=31)  # gzip wrapper

            def write_compressed(block):
                pending = memoryview(block)
                while pending:
                    wait_for_pipe(selector, deadline, age_command, timeout)
                    try:
                        pending = pending[os.write(age.stdin.fileno(), pending):]
                    except BlockingIOError:
                        continue
                    except BrokenPipeError as error:
                        raise RuntimeError(
                            f"age exited before encrypting the complete MariaDB dump for {path.name}",
                        ) from error

            stream_dump(
                command, path.name, lambda block: write_compressed(compressor.compress(block)),
                deadline, timeout,
            )
            write_compressed(compressor.flush())
            selector.unregister(age.stdin)
            age.stdin.close()
            code = age.wait(timeout=max(0, deadline - monotonic()))
            if code:
                raise RuntimeError(f"age exited with status {code} for {path.name}")
            if not os.fstat(ciphertext.fileno()).st_size:
                raise RuntimeError(f"age produced no ciphertext for {path.name}")
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"MariaDB dump and age for {path.name} timed out after {timeout} seconds") from error


def save_dump(command, path, timeout, age_command=None):
    deadline = monotonic() + timeout
    if age_command:
        save_encrypted_dump(command, path, age_command, deadline, timeout)
    else:
        with gzip.open(path, "xb") as output:
            stream_dump(command, path.name, output.write, deadline, timeout)


def wait_for_target(kubernetes):
    subprocess.run(
        kubernetes + ["wait", "--for=condition=Ready", POD, "--timeout=30s"],
        check=True,
        timeout=CLIENT_TIMEOUT_SECONDS,
    )


def check_target(kubernetes):
    wait_for_target(kubernetes)
    fields = subprocess.check_output(
        kubernetes + ["get", POD, "-o", POD_READINESS],
        text=True,
        timeout=CLIENT_TIMEOUT_SECONDS,
    ).split()
    if len(fields) != 2 or fields[1] != "True":
        raise RuntimeError("Could not confirm the identity of the ready MariaDB pod")
    return fields[0]


def backup(kubernetes, output_root, dump_timeout, age_command=None):
    wait_for_target(kubernetes)
    remote = kubernetes + ["exec", POD, "--container", "mariadb", "--", "sh", "-ec"]
    listed_schemas = subprocess.check_output(
        remote + [DISCOVER], text=True, timeout=CLIENT_TIMEOUT_SECONDS,
    )
    schemas = [
        name for name in listed_schemas.splitlines()
        if name and name.casefold() not in SYSTEM_SCHEMAS
    ]
    if not schemas:
        raise RuntimeError("MariaDB returned no application schemas")
    if any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_$-]*", name) for name in schemas):
        raise RuntimeError("MariaDB returned a schema name unsuitable for a backup filename")

    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output_root.is_symlink() or output_root.stat().st_mode & 0o077:
        raise RuntimeError("Use a private backup directory with no group or world permissions")
    started = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with TemporaryDirectory(prefix=f".mariadb-{started}-", dir=output_root) as scratch:
        stage = Path(scratch)
        extension = ".sql.gz.age" if age_command else ".sql.gz"
        for schema in schemas:
            save_dump(
                remote + [DUMP, "mariadb-dump", schema], stage / f"{schema}{extension}",
                dump_timeout, age_command,
            )
        destination = output_root / stage.name.removeprefix(".")
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("Backup destination already exists")
        stage.rename(destination)
    return destination


def cancel_backup(_signum, _frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise InterruptedError("interrupted by a termination signal")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kubeconfig", type=Path)
    parser.add_argument("context")
    parser.add_argument("namespace")
    parser.add_argument("output_root", type=Path, nargs="?", help="private local directory for backups")
    parser.add_argument(
        "--check-target", action="store_true", help="report the ready pod without accessing MariaDB",
    )
    parser.add_argument(
        "--dump-timeout-seconds", type=int, default=DEFAULT_DUMP_TIMEOUT_SECONDS,
        help="time limit per schema dump in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--age-recipients-file", type=Path,
        help="encrypt each backup with the native age X25519 public recipients in this file",
    )
    args = parser.parse_args()
    if not args.context.strip() or not args.namespace.strip():
        parser.error("context and namespace must be non-blank")
    if not args.kubeconfig.is_file():
        parser.error("kubeconfig must be an existing file")
    if not args.check_target and args.output_root is None:
        parser.error("output_root is required for a backup")
    if args.dump_timeout_seconds <= 0:
        parser.error("--dump-timeout-seconds must be positive")
    kubernetes = [
        "kubectl", "--kubeconfig", str(args.kubeconfig), "--context", args.context,
        "--namespace", args.namespace,
    ]
    os.umask(0o077)
    signal.signal(signal.SIGTERM, cancel_backup)
    signal.signal(signal.SIGINT, cancel_backup)
    operation = "Target check" if args.check_target else "Backup"
    try:
        if args.check_target:
            uid = check_target(kubernetes)
            checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            print(f"{checked_at} MariaDB target ready context={args.context} "
                  f"namespace={args.namespace} {POD} uid={uid}")
            return 0
        age_command = read_age_command(args.age_recipients_file) if args.age_recipients_file else None
        destination = backup(kubernetes, args.output_root, args.dump_timeout_seconds, age_command)
    except subprocess.CalledProcessError as error:
        print(f"{operation} stopped after kubectl exited with status {error.returncode}", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired as error:
        print(f"{operation} stopped: kubectl timed out after {error.timeout} seconds", file=sys.stderr)
        return 1
    except (OSError, RuntimeError) as error:
        print(f"{operation} stopped: {error}", file=sys.stderr)
        return 1
    print(f"Completed local MariaDB backup: {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
