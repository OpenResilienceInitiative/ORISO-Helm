"""Restore one SQLite metadata archive into an isolated destination."""

from __future__ import annotations

from contextlib import closing
import errno
import gzip
import hashlib
import importlib.util
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


RESTORE_SCRIPT = Path(__file__).resolve().parents[1] / "files/signoz/metadata_restore.py"
SPEC = importlib.util.spec_from_file_location("metadata_restore", RESTORE_SCRIPT)
assert SPEC and SPEC.loader
RESTORE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RESTORE)


class MetadataRestoreTest(unittest.TestCase):
    def setUp(self) -> None:
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        self.destination = self.root / "isolated restore"
        self.destination.mkdir()
        database = self.root / "source.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript("""
                CREATE TABLE organizations (id TEXT PRIMARY KEY);
                CREATE TABLE users (id TEXT PRIMARY KEY);
                CREATE TABLE dashboard (id TEXT PRIMARY KEY);
                CREATE TABLE rule (id TEXT PRIMARY KEY);
                INSERT INTO dashboard VALUES ('synthetic-dashboard');
            """)
        self.archive = self.root / "signoz-20261107T023000Z.db.gz"
        self.archive.write_bytes(gzip.compress(database.read_bytes()))

    def run_restore(self, *options: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RESTORE_SCRIPT), str(self.archive), str(self.destination), *options],
            capture_output=True, text=True, check=False, timeout=10,
        )

    def assert_refused(self, message: str, *options: str) -> None:
        result = self.run_restore(*options)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn(message, result.stderr)

    def test_restores_and_checks_metadata_into_an_empty_filesystem_root(self) -> None:
        (self.destination / "lost+found").mkdir()
        original = self.archive.read_bytes()
        result = self.run_restore()
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.destination / "signoz.db"
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(f"{output.as_uri()}?mode=ro&immutable=1", uri=True)) as database:
            self.assertEqual(database.execute("SELECT id FROM dashboard").fetchall(), [("synthetic-dashboard",)])
        self.assertEqual(sorted(item.name for item in self.destination.iterdir()), ["lost+found", "signoz.db"])
        self.assertEqual(self.archive.read_bytes(), original)

    def test_accepts_the_legacy_dashboard_and_rule_table_names(self) -> None:
        database = self.root / "source.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript("""
                ALTER TABLE dashboard RENAME TO dashboards;
                ALTER TABLE rule RENAME TO rules;
            """)
        self.archive.write_bytes(gzip.compress(database.read_bytes()))
        result = self.run_restore()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.destination / "signoz.db").is_file())

    def test_truncated_gzip_leaves_no_published_database_or_staging_file(self) -> None:
        self.archive.write_bytes(self.archive.read_bytes()[:-4])
        self.assert_refused("complete gzip archive")
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_refuses_a_valid_database_recompressed_under_an_attested_name(self) -> None:
        original = self.archive.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        name = (
            "signoz-20261107T023000Z-75a198df-bb28-4e9e-9b0b-9e038b5d713f"
            f"-cf829cd6-8339-4b58-a912-81a3ef109a57-{digest}.db.gz"
        )
        self.archive = self.archive.rename(self.root / name)
        database = self.root / "source.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("UPDATE dashboard SET id='different-dashboard'")
            connection.commit()
        self.archive.write_bytes(gzip.compress(database.read_bytes()))
        self.assert_refused("SHA-256")
        self.assert_refused("SHA-256", "--sha256", digest)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_refuses_a_receipt_with_the_wrong_compressed_byte_count(self) -> None:
        contents = self.archive.read_bytes()
        self.assert_refused(
            "byte count",
            "--sha256", hashlib.sha256(contents).hexdigest(),
            "--compressed-bytes", str(len(contents) + 1),
        )
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_rejects_a_valid_sqlite_database_without_signoz_metadata(self) -> None:
        database = self.root / "foreign.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")
        self.archive.write_bytes(gzip.compress(database.read_bytes()))
        self.assert_refused("SigNoz metadata tables")
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_rejects_bad_sqlite_contents_inside_a_valid_gzip(self) -> None:
        self.archive.write_bytes(gzip.compress(b"this is not a database"))
        self.assert_refused("SQLite")
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_leaves_existing_database_and_wal_untouched(self) -> None:
        database = self.destination / "signoz.db"
        wal = self.destination / "signoz.db-wal"
        database.write_bytes(b"existing database")
        wal.write_bytes(b"existing WAL")
        self.assert_refused("empty")
        self.assertEqual(database.read_bytes(), b"existing database")
        self.assertEqual(wal.read_bytes(), b"existing WAL")
        self.assertEqual(sorted(item.name for item in self.destination.iterdir()), ["signoz.db", "signoz.db-wal"])


    def test_a_stale_wal_alone_blocks_the_restore(self) -> None:
        wal = self.destination / "signoz.db-wal"
        wal.write_bytes(b"orphan WAL")
        self.assert_refused("empty")
        self.assertEqual(list(self.destination.iterdir()), [wal])
        self.assertEqual(wal.read_bytes(), b"orphan WAL")

    def test_partial_write_cleans_up_without_publishing(self) -> None:
        def fail_after_write(_archive, output, *, length):
            output.write(b"part of the file")
            raise OSError(errno.ENOSPC, "disk full")

        with patch.object(RESTORE.shutil, "copyfileobj", side_effect=fail_after_write):
            with self.assertRaises(OSError):
                RESTORE.stage_metadata(self.archive, self.destination)
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertEqual(self.run_restore().returncode, 0)

    def test_atomic_publication_leaves_a_competing_destination_untouched(self) -> None:
        link = os.link
        def publish_competitor(source, destination):
            destination.write_bytes(b"competing database")
            link(source, destination)

        with patch.object(RESTORE.os, "link", side_effect=publish_competitor):
            with self.assertRaisesRegex(RESTORE.RestoreError, "already contains"):
                RESTORE.stage_metadata(self.archive, self.destination)
        output = self.destination / "signoz.db"
        self.assertEqual(list(self.destination.iterdir()), [output])
        self.assertEqual(output.read_bytes(), b"competing database")


if __name__ == "__main__":
    unittest.main()
