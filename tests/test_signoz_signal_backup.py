"""Check the native ClickHouse signal backup's verification and archive retention."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / "files/signoz/signal_backup.py"
SPEC = importlib.util.spec_from_file_location("signal_backup", SCRIPT)
assert SPEC and SPEC.loader
BACKUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKUP)
ID = "0b5c7c4e-2a43-4c58-9e0a-6f1d2b3c4d5e"


class SignalBackupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.sql: list[str] = []

    def clickhouse(self, statuses=("CREATING_BACKUP", "BACKUP_CREATED"), corrupt=False, backup_id=ID):
        """Fake ClickHouse: BACKUP ... ASYNC writes the zip, then system.backups reports each status in turn."""
        polls = iter(statuses)

        def query(sql: str) -> str:
            self.sql.append(sql)
            if sql.startswith("BACKUP"):
                name = sql.split("'")[3]
                with zipfile.ZipFile(self.root / name, "w") as archive:
                    archive.writestr(".backup", "<config/>")
                if corrupt:
                    data = bytearray((self.root / name).read_bytes())
                    data[data.index(b"<config/>")] ^= 1
                    (self.root / name).write_bytes(bytes(data))
                return json.dumps({"id": backup_id, "status": "CREATING_BACKUP"}) + "\n"
            status = next(polls)
            return "" if status is None else json.dumps({"status": status, "error": "disk full"}) + "\n"
        return mock.patch.multiple(BACKUP, query=mock.Mock(side_effect=query), POLL_SECONDS=0)

    def old(self, stamp: str, verified: bool = True) -> None:
        (self.root / f"signal-{stamp}.zip").write_bytes(b"x")
        if verified:
            (self.root / f"signal-{stamp}.zip.sha256").write_text("x")

    def names(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir())

    def test_backs_up_all_six_databases_and_keeps_newest_verified(self) -> None:
        for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
            self.old(stamp)
        self.old("20250101T000000Z", verified=False)  # leftover of a failed run
        (self.root / "lost+found").mkdir()
        with self.clickhouse():
            receipt = BACKUP.back_up(self.root, keep=2)
        self.assertTrue(all(f"DATABASE {db}" in self.sql[0] for db in BACKUP.DATABASES))
        self.assertIn("TO Disk('oriso_signal_backup'", self.sql[0])
        self.assertIn(" ASYNC ", self.sql[0])
        self.assertEqual(len(self.sql), 3, "one BACKUP and two status polls")
        self.assertEqual(receipt["backupId"], ID)
        self.assertEqual(receipt["kept"], ["signal-20260103T000000Z.zip", receipt["archive"]])
        self.assertEqual(self.names(), sorted([
            "lost+found", "signal-20260103T000000Z.zip", "signal-20260103T000000Z.zip.sha256",
            receipt["archive"], receipt["archive"] + ".sha256",
        ]))
        self.assertIn(receipt["sha256"], (self.root / (receipt["archive"] + ".sha256")).read_text())

    def test_any_failure_publishes_and_prunes_nothing(self) -> None:
        cases = (
            ({"statuses": ("BACKUP_FAILED",)}, SystemExit),
            ({"statuses": (None,)}, SystemExit),  # ClickHouse restarted mid-backup
            ({"corrupt": True}, SystemExit),
            ({"backup_id": "x'; DROP TABLE t"}, ValueError),
        )
        for kwargs, error in cases:
            with self.subTest(**kwargs):
                self.old("20260101T000000Z")
                self.old("20260102T000000Z")
                with self.clickhouse(**kwargs), self.assertRaises(error):
                    BACKUP.back_up(self.root, keep=1)
                self.assertEqual([n for n in self.names() if n.endswith(".sha256")],
                                 ["signal-20260101T000000Z.zip.sha256", "signal-20260102T000000Z.zip.sha256"])

    def test_rejects_non_positive_keep_before_backing_up(self) -> None:
        with self.clickhouse(), self.assertRaises(ValueError):
            BACKUP.back_up(self.root, keep=0)
        self.assertEqual(self.sql, [])


if __name__ == "__main__":
    unittest.main()
