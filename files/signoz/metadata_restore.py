"""Verify a SigNoz SQLite backup and stage it in an isolated destination."""

from __future__ import annotations

import argparse
from contextlib import closing
import gzip
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
from typing import BinaryIO
import zlib


ARCHIVE_STAMP = r"signoz-[0-9]{8}T[0-9]{6}Z"
UUID_PATTERN = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
SHA256_PATTERN = r"[0-9a-f]{64}"
BACKUP_ARCHIVE = re.compile(
    rf"{ARCHIVE_STAMP}-(?P<job>{UUID_PATTERN})-(?P<pod>{UUID_PATTERN})"
    rf"-(?P<sha256>{SHA256_PATTERN})\.db\.gz"
)
COPY_CHUNK_SIZE = 1024 * 1024


class RestoreError(RuntimeError):
    """A restore refusal with a message suitable for operator output."""


class CompressedReader:
    """Measure the compressed bytes consumed by gzip."""

    def __init__(self, source: BinaryIO) -> None:
        self.source = source
        self.sha256 = hashlib.sha256()
        self.size = 0

    def read(self, size: int = -1) -> bytes:
        data = self.source.read(size)
        self.sha256.update(data)
        self.size += len(data)
        return data


def sync_directory(path: Path) -> None:
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def check_metadata(path: Path) -> None:
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as database:
            database.execute("PRAGMA trusted_schema=OFF")
            if database.execute("PRAGMA integrity_check(1)").fetchone() != ("ok",):
                raise RestoreError("SQLite integrity check failed.")
            tables = {
                name for (name,) in database.execute(
                    "SELECT name FROM sqlite_schema WHERE type = 'table'"
                )
            }
    except sqlite3.Error:
        raise RestoreError("SQLite integrity check failed.") from None

    # Dashboard and rule tables were renamed in the SigNoz metadata migrations.
    required = ({"organizations"}, {"users"}, {"dashboard", "dashboards"}, {"rule", "rules"})
    if not all(tables & names for names in required):
        raise RestoreError("The backup is missing required SigNoz metadata tables.")


def stage_metadata(
    archive: Path, destination: Path, *, sha256: str | None = None,
    compressed_bytes: int | None = None,
) -> None:
    if not archive.is_file():
        raise RestoreError("Select an existing SigNoz backup file.")
    name = BACKUP_ARCHIVE.fullmatch(archive.name)
    if name:
        if sha256 is not None and sha256 != name["sha256"]:
            raise RestoreError("The SHA-256 in the receipt differs from the archive name.")
        sha256 = name["sha256"]
    if sha256 is not None and not re.fullmatch(SHA256_PATTERN, sha256):
        raise RestoreError("Supply a valid SHA-256 checksum.")
    if compressed_bytes is not None and compressed_bytes < 1:
        raise RestoreError("Supply a positive compressed byte count.")
    if (
        destination.is_symlink()
        or not destination.is_dir()
        or any(entry.name != "lost+found" for entry in destination.iterdir())
    ):
        raise RestoreError("The destination must be an empty, isolated directory.")

    with tempfile.NamedTemporaryFile(prefix=".signoz-restore-", dir=destination) as staged:
        try:
            flags = os.O_RDONLY | os.O_NONBLOCK
            if sha256 is not None:
                flags |= os.O_NOFOLLOW
            with os.fdopen(os.open(archive, flags), "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise RestoreError("Select a regular SigNoz backup file.")
                compressed = CompressedReader(source)
                with gzip.GzipFile(fileobj=compressed, mode="rb") as database:
                    shutil.copyfileobj(database, staged, length=COPY_CHUNK_SIZE)
        except (EOFError, gzip.BadGzipFile, zlib.error):
            raise RestoreError("Read a complete gzip archive before staging the database.") from None
        if sha256 is not None and compressed.sha256.hexdigest() != sha256:
            raise RestoreError("The archive does not match the expected SHA-256 checksum.")
        if compressed_bytes is not None and compressed.size != compressed_bytes:
            raise RestoreError("The archive does not match the expected compressed byte count.")
        staged.flush()
        os.fsync(staged.fileno())
        check_metadata(Path(staged.name))
        # A competing restore or operator-created database must never be replaced.
        try:
            os.link(staged.name, destination / "signoz.db")
        except FileExistsError:
            raise RestoreError("The destination already contains signoz.db.") from None

    sync_directory(destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="Gzipped metadata backup from the SigNoz backup claim.")
    parser.add_argument("destination", type=Path, help="Empty directory on the isolated recovery volume.")
    parser.add_argument("--sha256", help="Compressed archive checksum from the backup Pod receipt.")
    parser.add_argument("--compressed-bytes", type=int, help="Archive size from the backup Pod receipt.")
    arguments = parser.parse_args()
    try:
        stage_metadata(
            arguments.archive, arguments.destination,
            sha256=arguments.sha256, compressed_bytes=arguments.compressed_bytes,
        )
    except RestoreError as error:
        parser.exit(1, f"SigNoz metadata restore failed. {error}\n")
    except OSError:
        parser.exit(1, "SigNoz metadata restore failed. Check file access and target space.\n")
    print("Verified and staged signoz.db for isolated SigNoz recovery.")


if __name__ == "__main__":
    main()
