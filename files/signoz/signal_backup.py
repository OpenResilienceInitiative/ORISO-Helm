"""Write a native ClickHouse archive of the six SigNoz databases to the protected disk."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
import zipfile

DATABASES = ("signoz_analytics", "signoz_logs", "signoz_metadata", "signoz_meter", "signoz_metrics", "signoz_traces")
DISK = "oriso_signal_backup"
ARCHIVE = re.compile(r"signal-\d{8}T\d{6}Z\.zip")
BACKUP_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
POLL_SECONDS = 10


def query(sql: str) -> str:
    request = urllib.request.Request(os.environ["CLICKHOUSE_URL"], data=sql.encode(), headers={
        "X-ClickHouse-User": os.environ["CLICKHOUSE_USER"],
        "X-ClickHouse-Key": os.environ["CLICKHOUSE_PASSWORD"],
    })
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode()


def create(name: str) -> str:
    # ASYNC plus polling: a long BACKUP never depends on one HTTP request staying open.
    scope = ", ".join(f"DATABASE {database}" for database in DATABASES)
    backup_id = json.loads(query(f"BACKUP {scope} TO Disk('{DISK}', '{name}') ASYNC FORMAT JSONEachRow"))["id"]
    if not BACKUP_ID.fullmatch(backup_id):
        raise ValueError(f"unexpected backup id {backup_id!r}")
    while True:
        status = query(f"SELECT status, error FROM system.backups WHERE id = '{backup_id}' FORMAT JSONEachRow")
        if not status.strip():
            # system.backups lives in memory: a ClickHouse restart drops the running backup.
            raise SystemExit(f"ClickHouse signal backup {backup_id} disappeared; ClickHouse probably restarted.")
        row = json.loads(status)
        if row["status"] != "CREATING_BACKUP":
            break
        time.sleep(POLL_SECONDS)
    if row["status"] != "BACKUP_CREATED":
        raise SystemExit(f"ClickHouse signal backup {backup_id} ended with {row['status']}: {row['error'][:500]}")
    return backup_id


def back_up(root: Path, keep: int) -> dict:
    if keep < 1:
        raise ValueError("KEEP must be positive")
    name = datetime.now(timezone.utc).strftime("signal-%Y%m%dT%H%M%SZ.zip")
    backup_id = create(name)

    archive = root / name
    # Every member's CRC is read back before the archive counts as a recovery point.
    with zipfile.ZipFile(archive) as contents:
        if contents.testzip() is not None:
            raise SystemExit(f"ClickHouse signal archive {name} failed its CRC check.")
    with archive.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    (root / f"{name}.sha256").write_text(f"{digest}  {name}\n", encoding="utf-8")

    # Only verified archives (with a digest file) count toward retention. Older archives
    # without one are leftovers of failed runs (the CronJob never overlaps) and go too.
    archives = sorted(p for p in root.iterdir() if ARCHIVE.fullmatch(p.name))
    verified = [p for p in archives if (root / f"{p.name}.sha256").is_file()]
    for expired in verified[:-keep]:
        expired.unlink()
        (root / f"{expired.name}.sha256").unlink()
    for leftover in archives:
        if leftover.name < name and leftover not in verified:
            leftover.unlink()
    return {"version": 1, "backupId": backup_id, "archive": name, "sha256": digest,
            "bytes": archive.stat().st_size, "kept": [p.name for p in verified[-keep:]]}


def main() -> None:
    try:
        receipt = back_up(Path("/backup"), int(os.environ["KEEP"]))
    except urllib.error.HTTPError as error:
        raise SystemExit(f"ClickHouse refused the signal backup: {error.read().decode(errors='replace')[:500]}") from None
    except (KeyError, OSError, ValueError, zipfile.BadZipFile) as error:
        raise SystemExit(f"ClickHouse signal backup failed: {error}") from None
    Path("/dev/termination-log").write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
