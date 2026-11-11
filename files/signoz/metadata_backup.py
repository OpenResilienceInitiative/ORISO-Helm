"""Publish a verified SigNoz SQLite archive and report it to Kubernetes."""

from contextlib import closing
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import zlib

from restore import (
    ARCHIVE_STAMP, BACKUP_ARCHIVE, COPY_CHUNK_SIZE, UUID_PATTERN,
    CompressedReader, RestoreError, check_metadata, sync_directory,
)


def make_archive(source: Path, scratch: Path) -> tuple[Path, str, int]:
    snapshot = scratch / "signoz.db"
    with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as live:
        with closing(sqlite3.connect(snapshot)) as copy:
            live.backup(copy)
    check_metadata(snapshot)

    part = scratch / "archive.db.gz"
    with snapshot.open("rb") as database, gzip.open(part, "wb") as output:
        shutil.copyfileobj(database, output, length=COPY_CHUNK_SIZE)
    part.chmod(0o600)
    with snapshot.open("rb") as database:
        expected = hashlib.file_digest(database, "sha256").digest()
    with part.open("rb") as raw:
        os.fsync(raw.fileno())
        compressed = CompressedReader(raw)
        with gzip.GzipFile(fileobj=compressed, mode="rb") as contents:
            if hashlib.file_digest(contents, "sha256").digest() != expected:
                raise RestoreError("The gzip archive does not match the validated SQLite snapshot.")
    return part, compressed.sha256.hexdigest(), compressed.size


def back_up(
    source: Path, destination: Path, receipt_path: Path, *, job_uid: str, pod_uid: str, keep: int,
) -> None:
    if not all(re.fullmatch(UUID_PATTERN, uid) for uid in (job_uid, pod_uid)) or keep < 1:
        raise RestoreError("Supply the Job and Pod UIDs and a positive archive retention count.")

    # Keep the lock inode in place and reserve a staging path outside the legacy cleanup glob.
    lock_path = destination / ".signoz_backup.lock"
    with os.fdopen(os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "wb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RestoreError("Another metadata backup is writing to this claim.") from None

        previous = []
        for entry in destination.iterdir():
            if entry.is_symlink() or not entry.is_file():
                continue
            archive = BACKUP_ARCHIVE.fullmatch(entry.name)
            if archive and archive["job"] == job_uid and archive["pod"] == pod_uid:
                raise RestoreError("This Pod already published a metadata archive.")
            if archive or re.fullmatch(rf"{ARCHIVE_STAMP}\.db\.gz", entry.name):
                previous.append(entry)

        scratch = destination / ".signoz_metadata_stage"
        if scratch.exists():
            shutil.rmtree(scratch)
        scratch.mkdir(mode=0o700)
        try:
            part, digest, size = make_archive(source, scratch)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            name = f"signoz-{stamp}-{job_uid}-{pod_uid}-{digest}.db.gz"
            os.link(part, destination / name)
            sync_directory(destination)
        finally:
            shutil.rmtree(scratch)

        receipt_path.write_text(json.dumps({
            "version": 1, "archive": name, "jobUid": job_uid, "podUid": pod_uid,
            "sha256": digest, "bytes": size,
        }) + "\n", encoding="utf-8")
        for expired in sorted(previous, reverse=True)[keep - 1:]:
            expired.unlink()
        sync_directory(destination)
        print("Published a verified SigNoz metadata archive.")


def main() -> None:
    try:
        back_up(
            Path("/signoz/signoz.db"), Path("/backup"), Path("/dev/termination-log"),
            job_uid=os.environ["JOB_UID"], pod_uid=os.environ["POD_UID"], keep=int(os.environ["KEEP"]),
        )
    except RestoreError as error:
        raise SystemExit(f"SigNoz metadata backup failed. {error}") from None
    except (EOFError, KeyError, OSError, ValueError, sqlite3.Error, zlib.error):
        raise SystemExit("SigNoz metadata backup failed. Check backup settings, file access and target space.") from None


if __name__ == "__main__":
    main()
