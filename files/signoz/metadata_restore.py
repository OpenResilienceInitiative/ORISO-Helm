"""Verify a SigNoz SQLite backup and stage it in an isolated destination."""

from __future__ import annotations

import argparse
from contextlib import closing
import gzip
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zlib


class RestoreError(RuntimeError):
    """A restore refusal with a message suitable for operator output."""


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


def stage_metadata(archive: Path, destination: Path) -> None:
    if not archive.is_file():
        raise RestoreError("Select an existing SigNoz backup file.")
    if (
        destination.is_symlink()
        or not destination.is_dir()
        or any(entry.name != "lost+found" for entry in destination.iterdir())
    ):
        raise RestoreError("The destination must be an empty, isolated directory.")

    with tempfile.NamedTemporaryFile(prefix=".signoz-restore-", dir=destination) as staged:
        try:
            with gzip.open(archive, "rb") as compressed:
                shutil.copyfileobj(compressed, staged, length=1024 * 1024)
        except (EOFError, gzip.BadGzipFile, zlib.error):
            raise RestoreError("Read a complete gzip archive before staging the database.") from None
        staged.flush()
        os.fsync(staged.fileno())
        check_metadata(Path(staged.name))
        # A competing restore or operator-created database must never be replaced.
        try:
            os.link(staged.name, destination / "signoz.db")
        except FileExistsError:
            raise RestoreError("The destination already contains signoz.db.") from None

    directory = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="Gzipped metadata backup from the SigNoz backup claim.")
    parser.add_argument("destination", type=Path, help="Empty directory on the isolated recovery volume.")
    arguments = parser.parse_args()
    try:
        stage_metadata(arguments.archive, arguments.destination)
    except RestoreError as error:
        parser.exit(1, f"SigNoz metadata restore failed. {error}\n")
    except OSError:
        parser.exit(1, "SigNoz metadata restore failed. Check file access and target space.\n")
    print("Verified and staged signoz.db for isolated SigNoz recovery.")


if __name__ == "__main__":
    main()
