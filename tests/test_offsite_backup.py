"""Exercise the off-site copy, retention and freshness check against a fake sftp client."""

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import signoz_check_journal

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "offsite_backup.py"
spec = importlib.util.spec_from_file_location("offsite_backup", SCRIPT)
offsite = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offsite)

rspec = importlib.util.spec_from_file_location("offsite_restore_test", ROOT / "scripts" / "offsite_restore_test.py")
restore = importlib.util.module_from_spec(rspec)
rspec.loader.exec_module(restore)

FAKE_SFTP = r'''#!/usr/bin/env python3
import os, shlex, shutil, sys
from pathlib import Path
root = Path(os.environ["FAKE_BOX"])
mode = os.environ.get("FAKE_MODE", "ok")
fail_put = mode == "short-put"
for raw in sys.stdin.read().splitlines():
    soft = raw.startswith("-")
    parts = shlex.split(raw.lstrip("-"))
    cmd, args = parts[0], parts[1:]
    open(os.environ["FAKE_LOG"], "a").write(" ".join(parts) + "\n")
    try:
        if mode == "down":
            raise OSError("unreachable")
        if cmd == "mkdir": (root / args[0]).mkdir()
        elif cmd == "put":
            data = Path(args[0]).read_bytes()
            (root / args[1]).write_bytes(data[:1] if fail_put and args[1].endswith(".age") else data)
        elif cmd == "get": Path(args[1]).write_bytes((root / args[0]).read_bytes())
        elif cmd == "rename": (root / args[0]).rename(root / args[1])
        elif cmd == "rm": (root / args[0]).unlink()
        elif cmd == "rmdir": (root / args[0]).rmdir()
        elif cmd == "ls":
            target = root / args[-1]
            if not target.is_dir(): raise OSError("no such directory")
            for item in sorted(target.iterdir()):
                if "-l" in args:
                    print(f"-rw-------    ? 1001 100 {item.stat().st_size:>8} Nov 21 13:05 {args[-1]}/{item.name}")
                else:
                    print(f"{args[-1]}/{item.name}")
    except OSError:
        if not soft: sys.exit(1)
'''


def stamp(when):
    return when.strftime("%Y%m%dT%H%M%SZ")


class OffsiteBackupTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.box, self.local, self.bin = (self.root / n for n in ("box", "local", "bin"))
        for folder in (self.box, self.local, self.bin):
            folder.mkdir()
        (self.bin / "sftp").write_text(FAKE_SFTP)
        (self.bin / "sftp").chmod(0o700)
        self.log = self.root / "sftp.log"
        self.config = {
            "storageBox": {"host": "box.example", "user": "u1", "identityFile": "/key",
                           "knownHostsFile": "/hosts", "remoteBase": "oriso-dev"},
            "sources": [{"prefix": "mariadb", "root": str(self.local / "mariadb")},
                        {"prefix": "mongodb", "root": str(self.local / "mongodb")}],
            "retention": {"daily": 3, "weekly": 2, "maxAgeHours": 36, "localKeepDays": 5},
        }
        for prefix in ("mariadb", "mongodb"):
            (self.local / prefix).mkdir()
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(self.config))
        self.now = datetime(2026, 11, 21, 3, 0, tzinfo=timezone.utc)

    def add_run(self, prefix, when, payload=b"ciphertext"):
        run = self.local / prefix / f"{prefix}-{stamp(when)}-abc123"
        run.mkdir()
        (run / "dump.sql.gz.age").write_bytes(b"age-encryption.org/v1\n" + payload)
        return run

    def sftp(self, mode="ok"):
        env = {"FAKE_BOX": str(self.box), "FAKE_LOG": str(self.log), "FAKE_MODE": mode}
        def runner(command, **options):
            return subprocess.run([sys.executable, str(self.bin / "sftp"), *command[1:]],
                                  env={**os.environ, **env}, **options)
        return offsite.Sftp(self.config["storageBox"], runner)

    def remote(self, prefix):
        path = self.box / "oriso-dev" / prefix
        return sorted(p.name for p in path.iterdir()) if path.exists() else []

    def test_retention_keeps_30_daily_and_12_weekly_runs(self):
        start = datetime(2026, 1, 1, 2, tzinfo=timezone.utc)
        runs = [(f"r{n}", start + timedelta(days=n)) for n in range(200)]
        runs.append(("same-day-older", start + timedelta(days=199, hours=-1)))
        keep = offsite.select_keep(runs, 30, 12)
        daily = {f"r{n}" for n in range(170, 200)}
        self.assertTrue(daily <= keep)
        self.assertNotIn("same-day-older", keep)
        older = keep - daily
        # Beyond the daily window only the newest run of each remaining ISO week survives.
        weeks = [tuple((start + timedelta(days=int(n[1:]))).isocalendar())[:2] for n in older]
        self.assertEqual(len(weeks), len(set(weeks)))
        self.assertTrue(all((start + timedelta(days=int(n[1:]) + 1)).isocalendar()[1]
                            != (start + timedelta(days=int(n[1:]))).isocalendar()[1] for n in older))
        self.assertEqual(len(keep), 30 + len(older))
        self.assertLessEqual(len(older), 12)
        self.assertNotIn("r0", keep)

    def test_push_uploads_every_complete_run_with_a_receipt(self):
        archive_at = self.now - timedelta(hours=1)
        self.add_run("mariadb", archive_at)
        self.add_run("mongodb", archive_at - timedelta(days=1))
        self.add_run("mongodb", archive_at)
        report, problems = offsite.push(offsite.load_config(self.config_path), self.sftp(), self.now)
        self.assertEqual(problems, [])
        self.assertEqual(report["mariadb"]["uploaded"], 1)
        self.assertEqual(report["mongodb"]["uploaded"], 2)
        for entry in report.values():
            self.assertEqual(entry["archiveAt"], archive_at.isoformat())
            self.assertIs(entry["complete"], True)
        (run,) = self.remote("mariadb")
        files = sorted(p.name for p in (self.box / "oriso-dev" / "mariadb" / run).iterdir())
        self.assertEqual(files, ["SHA256SUMS", "dump.sql.gz.age"])
        # A second run uploads nothing and is idempotent.
        report, problems = offsite.push(offsite.load_config(self.config_path), self.sftp(), self.now)
        self.assertEqual((problems, report["mariadb"]["uploaded"]), ([], 0))
        for entry in report.values():
            self.assertNotIn("archiveAt", entry)
            self.assertNotIn("complete", entry)
        self.assertEqual(len(self.remote("mariadb")), 1)

    def test_verified_copy_updates_alert_evidence_until_the_next_check(self):
        config = offsite.load_config(self.config_path)
        sftp = self.sftp()
        check_path, push_path = self.root / "check.json", self.root / "push.json"

        def record(command, operation, when, path):
            report, problems = operation(config, sftp, when)
            offsite.write_status_file(path, {
                "command": command, "at": when.isoformat(), "ok": not problems,
                "report": report, "problems": problems,
            })

        for prefix in ("mariadb", "mongodb"):
            self.add_run(prefix, self.now - timedelta(hours=1))
        record("push", offsite.push, self.now, push_path)
        record("check", offsite.check, self.now + timedelta(hours=5), check_path)

        copy_time = self.now + timedelta(days=1)
        between_jobs = copy_time + timedelta(hours=1, minutes=30)
        stale = signoz_check_journal.read_backup_freshness(
            check_path, push_path=push_path, now=between_jobs,
        )
        self.assertEqual(stale["stores"]["mariadb"]["reason"], "stale-archive")

        run = self.add_run("mariadb", copy_time - timedelta(minutes=30))
        record("push", offsite.push, copy_time, push_path)
        fresh = signoz_check_journal.read_backup_freshness(
            check_path, push_path=push_path, now=between_jobs,
        )
        self.assertEqual(fresh["stores"]["mariadb"]["reason"], "fresh-archive")
        self.assertEqual(fresh["stores"]["mongodb"]["reason"], "stale-archive")

        record("push", offsite.push, copy_time + timedelta(hours=1), push_path)
        no_op = json.loads(push_path.read_text())
        self.assertEqual(no_op["report"]["mariadb"]["uploaded"], 0)
        after_no_op = signoz_check_journal.read_backup_freshness(
            check_path, push_path=push_path, now=between_jobs,
        )
        self.assertEqual(after_no_op, fresh)

        (self.box / "oriso-dev" / "mariadb" / run.name / "SHA256SUMS").unlink()
        check_time = copy_time + timedelta(hours=5)
        record("check", offsite.check, check_time, check_path)
        after_check = signoz_check_journal.read_backup_freshness(
            check_path, push_path=push_path, now=check_time,
        )
        self.assertEqual(after_check["probe"]["reason"], "probe-failed")
        self.assertEqual(after_check["stores"]["mariadb"]["reason"], "incomplete-archive")

        # Another no-op must not give the older upload a newer verification time.
        after_check_no_op = check_time + timedelta(hours=1)
        record("push", offsite.push, after_check_no_op, push_path)
        self.assertEqual(
            signoz_check_journal.read_backup_freshness(
                check_path, push_path=push_path, now=after_check_no_op,
            )["stores"]["mariadb"],
            after_check["stores"]["mariadb"],
        )
        carried = json.loads(push_path.read_text())["report"]["mariadb"]
        self.assertEqual(carried["verifiedAt"], copy_time.isoformat())

    def test_verified_upload_survives_failed_pushes_until_the_next_successful_no_op(self):
        config = offsite.load_config(self.config_path)
        check_path, push_path = self.root / "check.json", self.root / "push.json"

        def record(command, operation, when, path, mode="ok"):
            report, problems = operation(config, self.sftp(mode), when)
            offsite.write_status_file(path, {
                "command": command, "at": when.isoformat(), "ok": not problems,
                "report": report, "problems": problems,
            })
            return json.loads(path.read_text())

        yesterday = self.now - timedelta(days=1)
        for prefix in ("mariadb", "mongodb"):
            self.add_run(prefix, yesterday - timedelta(hours=1))
        record("push", offsite.push, yesterday, push_path)
        record("check", offsite.check, yesterday + timedelta(hours=5), check_path)

        archive_at = self.now - timedelta(minutes=30)
        self.add_run("mariadb", archive_at)
        record("push", offsite.push, self.now, push_path)
        observed = self.now + timedelta(hours=1, minutes=30)
        fresh = signoz_check_journal.read_backup_freshness(
            check_path, push_path=push_path, now=observed,
        )
        self.assertEqual(fresh["stores"]["mariadb"]["reason"], "fresh-archive")

        for minutes in (10, 20):
            failed = record("push", offsite.push, self.now + timedelta(minutes=minutes),
                            push_path, mode="down")
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["report"], {})
            self.assertTrue(failed["problems"])
            self.assertEqual(
                failed["carriedUploads"]["mariadb"]["verifiedAt"], self.now.isoformat(),
            )
            current_archive = signoz_check_journal.read_backup_freshness(
                check_path, push_path=push_path, now=observed,
            )["stores"]["mariadb"]
            self.assertEqual(
                (current_archive["state"], current_archive["reason"]),
                ("CRITICAL", "stale-archive"),
            )

        resumed = record("push", offsite.push, self.now + timedelta(minutes=30), push_path)
        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["report"]["mariadb"]["uploaded"], 0)
        self.assertEqual(
            signoz_check_journal.read_backup_freshness(
                check_path, push_path=push_path, now=observed,
            ),
            fresh,
        )
        self.assertEqual(resumed["report"]["mariadb"]["verifiedAt"], self.now.isoformat())

    def test_no_op_does_not_turn_an_invalid_timestamp_into_verified_evidence(self):
        path = self.root / "push.json"
        old = {
            "command": "push", "at": self.now.isoformat(), "ok": True,
            "report": {"mariadb": {
                "uploaded": 1, "archiveAt": (self.now - timedelta(hours=1)).isoformat(sep=" "),
                "complete": True,
            }},
        }
        path.write_text(json.dumps(old))
        current = {
            "command": "push", "at": (self.now + timedelta(hours=1)).isoformat(), "ok": True,
            "report": {"mariadb": {"uploaded": 0}},
        }
        offsite.write_status_file(path, current)
        self.assertEqual(json.loads(path.read_text())["report"]["mariadb"], {"uploaded": 0})

    def test_push_does_not_publish_evidence_for_a_future_archive(self):
        self.add_run("mariadb", self.now + timedelta(hours=1))
        report, problems = offsite.push(offsite.load_config(self.config_path), self.sftp(), self.now)
        self.assertEqual(problems, [])
        self.assertEqual(report["mariadb"]["uploaded"], 1)
        self.assertNotIn("archiveAt", report["mariadb"])
        self.assertNotIn("complete", report["mariadb"])

    def test_a_short_upload_is_not_published(self):
        self.add_run("mariadb", self.now - timedelta(hours=1), b"x" * 100)
        report, problems = offsite.push(offsite.load_config(self.config_path), self.sftp("short-put"), self.now)
        self.assertTrue(any("Size check failed" in p for p in problems), problems)
        self.assertFalse([n for n in self.remote("mariadb") if not n.startswith(".")])

    def test_unreachable_box_deletes_nothing_locally_or_remotely(self):
        run = self.add_run("mariadb", self.now - timedelta(days=30))
        _, problems = offsite.push(offsite.load_config(self.config_path), self.sftp("down"), self.now)
        self.assertTrue(problems)
        self.assertTrue(run.exists())

    def test_prune_removes_only_runs_outside_the_ladder_and_keeps_unknown_names(self):
        base = self.box / "oriso-dev" / "mariadb"
        base.mkdir(parents=True)
        for n in range(8):
            run = base / f"mariadb-{stamp(self.now - timedelta(days=n, hours=1))}"
            run.mkdir()
            (run / "a.age").write_bytes(b"1")
        (base / "notes").mkdir()
        (base / ".mariadb-20260101T000000Z.partial").mkdir()
        removed = offsite.prune_remote(self.sftp(), "oriso-dev", "mariadb", self.config["retention"], 60)
        self.assertEqual(removed, 4)
        kept = self.remote("mariadb")
        self.assertIn("notes", kept)
        self.assertEqual(len([n for n in kept if n.startswith("mariadb-")]), 4)
        self.assertIn(f"mariadb-{stamp(self.now - timedelta(hours=1))}", kept)

    def test_prune_refuses_a_mass_deletion(self):
        base = self.box / "oriso-dev" / "mariadb"
        base.mkdir(parents=True)
        for n in range(30):
            (base / f"mariadb-{stamp(self.now - timedelta(days=n * 7))}").mkdir()
        with self.assertRaises(offsite.OffsiteError):
            offsite.prune_remote(self.sftp(), "oriso-dev", "mariadb", self.config["retention"], 5)

    def test_local_copies_are_removed_only_after_they_are_off_site_and_old(self):
        old = self.add_run("mariadb", self.now - timedelta(days=9))
        young = self.add_run("mariadb", self.now - timedelta(days=1))
        offsite.push(offsite.load_config(self.config_path), self.sftp(), self.now)
        self.assertFalse(old.exists())
        self.assertTrue(young.exists())

    def test_runs_outside_the_ladder_are_neither_uploaded_nor_kept_locally_forever(self):
        recent = self.add_run("mariadb", self.now - timedelta(hours=1))
        expired = [self.add_run("mariadb", self.now - timedelta(days=n)) for n in range(20, 26)]
        config = offsite.load_config(self.config_path)
        offsite.push(config, self.sftp(), self.now)
        kept = offsite.select_keep(
            [(run.name, offsite.run_stamp(run.name)) for run in [recent, *expired]], 3, 2)
        self.assertEqual(sorted(self.remote("mariadb")), sorted(kept))
        self.assertTrue(recent.exists())
        self.assertFalse(any(run.exists() for run in expired))
        report, problems = offsite.push(config, self.sftp(), self.now)
        self.assertEqual((problems, report["mariadb"]["uploaded"], report["mariadb"]["prunedRemote"]), ([], 0, 0))

    def test_restore_test_downloads_the_newest_run_and_verifies_its_receipt(self):
        older = self.add_run("mongodb", self.now - timedelta(days=2), b"old")
        self.add_run("mongodb", self.now - timedelta(hours=1), b"new-ciphertext")
        offsite.push(offsite.load_config(self.config_path), self.sftp(), self.now)
        run, files = restore.fetch_newest(self.sftp(), "oriso-dev", "mongodb", self.root / "fetched")
        self.assertNotEqual(run, older.name)
        self.assertEqual([p.read_bytes() for p in files], [b"age-encryption.org/v1\nnew-ciphertext"])
        self.assertEqual(oct((self.root / "fetched").stat().st_mode & 0o777), "0o700")

    def test_restore_test_rejects_a_tampered_archive(self):
        self.add_run("mongodb", self.now - timedelta(hours=1), b"genuine")
        offsite.push(offsite.load_config(self.config_path), self.sftp(), self.now)
        (run,) = self.remote("mongodb")
        (self.box / "oriso-dev" / "mongodb" / run / "dump.sql.gz.age").write_bytes(b"swapped!")
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            restore.fetch_newest(self.sftp(), "oriso-dev", "mongodb", self.root / "fetched")

    def test_copy_and_pruning_ignore_runs_from_a_different_store(self):
        config = offsite.load_config(self.config_path)
        config["retention"].update(daily=1, weekly=1)
        local = self.add_run("mariadb", self.now - timedelta(hours=2))
        target = self.box / "oriso-dev" / "mariadb"
        target.mkdir(parents=True)
        foreign_runs = [
            target / f"mongodb-{stamp(self.now - timedelta(hours=1))}",
            target / f"mongodb-{stamp(self.now - timedelta(days=14))}",
        ]
        retired = target / f"mariadb-{stamp(self.now - timedelta(days=7))}"
        for run in [*foreign_runs, retired]:
            run.mkdir()
            (run / "dump.sql.gz.age").write_bytes(b"existing fixture")

        report, problems = offsite.push(config, self.sftp(), self.now)

        self.assertEqual(problems, [])
        self.assertEqual(report["mariadb"]["uploaded"], 1)
        self.assertEqual(report["mariadb"]["prunedRemote"], 1)
        self.assertTrue((target / local.name).is_dir())
        self.assertTrue(all(run.is_dir() for run in foreign_runs))
        self.assertFalse(retired.exists())

    def test_restore_ignores_runs_from_a_different_store(self):
        valid = self.add_run("mariadb", self.now - timedelta(hours=2))
        config = offsite.load_config(self.config_path)
        offsite.push(config, self.sftp(), self.now)
        target = self.box / "oriso-dev" / "mariadb"
        foreign = target / f"mongodb-{stamp(self.now - timedelta(hours=1))}"
        foreign.mkdir()
        for file in (target / valid.name).iterdir():
            (foreign / file.name).write_bytes(file.read_bytes())

        restored, _ = restore.fetch_newest(self.sftp(), "oriso-dev", "mariadb", self.root / "fetched")

        self.assertEqual(restored, valid.name)

    def test_units_and_example_configuration_match_the_script(self):
        import configparser
        import shlex
        units = ROOT / "runbooks" / "systemd"
        for name, verb in (("oriso-offsite-backup", "push"), ("oriso-offsite-check", "check")):
            parser = configparser.ConfigParser(strict=False, interpolation=None)
            parser.optionxform = str
            parser.read(units / f"{name}.service")
            command = shlex.split(parser["Service"]["ExecStart"])
            self.assertEqual(command[:4], ["/usr/bin/python3", "-I", "/opt/oriso/offsite-backup/offsite_backup.py", verb])
            self.assertIn("/etc/oriso/offsite-backup.json", command)
            self.assertEqual(parser["Service"]["UMask"], "0077")
            timer = configparser.ConfigParser(strict=False, interpolation=None)
            timer.optionxform = str
            timer.read(units / f"{name}.timer")
            self.assertEqual(timer["Timer"]["Persistent"], "true")
        config = offsite.load_config(ROOT / "runbooks" / "offsite-backup.example.json")
        self.assertEqual((config["retention"]["daily"], config["retention"]["weekly"]), (30, 12))
        self.assertEqual({s["prefix"] for s in config["sources"]}, {"mariadb", "mongodb", "matrix-postgres"})
        self.assertEqual(config["storageBox"]["port"], 23)

    def test_check_reports_typed_evidence_for_fresh_and_stale_archives(self):
        config = offsite.load_config(self.config_path)
        newest, problems = offsite.check(config, self.sftp(), self.now)
        self.assertEqual(newest, {})
        self.assertEqual(len(problems), 2)
        fresh_at = self.now - timedelta(hours=2)
        stale_at = self.now - timedelta(hours=60)
        fresh = self.add_run("mariadb", fresh_at)
        stale = self.add_run("mongodb", stale_at)
        offsite.push(config, self.sftp(), self.now)
        newest, problems = offsite.check(config, self.sftp(), self.now)
        self.assertEqual(newest, {
            "mariadb": {"run": fresh.name, "ageHours": 2.0,
                        "archiveAt": fresh_at.isoformat(), "complete": True},
            "mongodb": {"run": stale.name, "ageHours": 60.0,
                        "archiveAt": stale_at.isoformat(), "complete": True},
        })
        self.assertEqual(len(problems), 1)
        self.assertIn("mongodb", problems[0])

    def test_check_ignores_invalid_calendar_cross_prefix_and_future_runs(self):
        config = offsite.load_config(self.config_path)
        valid_at = self.now - timedelta(hours=2)
        valid = self.add_run("mariadb", valid_at)
        offsite.push(config, self.sftp(), self.now)
        target = self.box / "oriso-dev" / "mariadb"
        accepted = target / valid.name
        invalid_names = (
            f"mariadb-{stamp(self.now + timedelta(seconds=1))}",
            f"mongodb-{stamp(self.now - timedelta(hours=1))}",
            "mariadb-20261131T030000Z",
        )
        for name in invalid_names:
            run = target / name
            run.mkdir()
            for file in accepted.iterdir():
                (run / file.name).write_bytes(file.read_bytes())

        newest, problems = offsite.check(config, self.sftp(), self.now)
        self.assertEqual(newest["mariadb"]["run"], valid.name)
        self.assertEqual(problems, ["mongodb: no off-site run"])
        self.assertNotIn("mongodb", newest)

        for file in accepted.iterdir():
            file.unlink()
        accepted.rmdir()
        newest, problems = offsite.check(config, self.sftp(), self.now)
        self.assertEqual(newest, {})
        self.assertEqual(problems, ["mariadb: no off-site run", "mongodb: no off-site run"])

    def test_check_marks_newest_archive_incomplete_without_falling_back(self):
        config = offsite.load_config(self.config_path)
        fresh_at = self.now - timedelta(hours=2)
        older = self.add_run("mariadb", fresh_at - timedelta(days=1))
        fresh = self.add_run("mariadb", fresh_at)
        self.add_run("mongodb", fresh_at)
        offsite.push(config, self.sftp(), self.now)
        self.assertCountEqual(self.remote("mariadb"), [older.name, fresh.name])
        target = self.box / "oriso-dev" / "mariadb" / fresh.name
        receipt = target / "SHA256SUMS"
        ciphertext = target / "dump.sql.gz.age"
        receipt_data = receipt.read_bytes()
        ciphertext_data = ciphertext.read_bytes()
        for problem in ("missing receipt", "empty receipt", "missing archive", "empty archive"):
            with self.subTest(problem=problem):
                receipt.write_bytes(receipt_data)
                ciphertext.write_bytes(ciphertext_data)
                file = receipt if "receipt" in problem else ciphertext
                if problem.startswith("missing"):
                    file.unlink()
                else:
                    file.write_bytes(b"")
                newest, problems = offsite.check(config, self.sftp(), self.now)
                self.assertEqual(newest["mariadb"], {
                    "run": fresh.name, "ageHours": 2.0,
                    "archiveAt": fresh_at.isoformat(), "complete": False,
                })
                self.assertTrue(newest["mongodb"]["complete"])
                self.assertEqual(problems, [f"mariadb: newest off-site run {fresh.name} is incomplete"])

    def test_check_timeout_suppresses_ssh_arguments_in_status(self):
        status = self.root / "status.json"
        timeout = subprocess.TimeoutExpired(["sftp", "-i", "/private/synthetic-identity"], 12)
        with patch.object(offsite, "check", side_effect=timeout), redirect_stdout(io.StringIO()) as output:
            result = offsite.main(["check", "--config", str(self.config_path), "--status-file", str(status)])
        self.assertEqual(result, 1)
        self.assertNotIn("/private/synthetic-identity", output.getvalue())
        self.assertNotIn("/private/synthetic-identity", status.read_text())
        self.assertFalse(json.loads(status.read_text())["ok"])

    def test_failed_status_replace_preserves_last_complete_status(self):
        status = self.root / "status.json"
        status.write_text('{"ok":true}')
        with patch.object(Path, "replace", side_effect=OSError("synthetic replacement failure")):
            with self.assertRaises(OSError):
                offsite.write_status_file(status, {"ok": False})
        self.assertEqual(status.read_text(), '{"ok":true}')
        self.assertEqual(list(self.root.glob(".status.json.*")), [])

    def test_cli_writes_a_status_file_and_the_exit_code_follows_it(self):
        env = {**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
               "FAKE_BOX": str(self.box), "FAKE_LOG": str(self.log)}
        status = self.root / "status.json"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "check", "--config", str(self.config_path), "--status-file", str(status)],
            env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(status.read_text())["ok"])

    def test_configuration_rejects_unsafe_values(self):
        for change in ({"storageBox": {**self.config["storageBox"], "remoteBase": "../x"}},
                       {"storageBox": {**self.config["storageBox"], "remoteBase": "/abs"}},
                       {"sources": []}):
            self.config_path.write_text(json.dumps({**self.config, **change}))
            with self.assertRaises(offsite.OffsiteError):
                offsite.load_config(self.config_path)

    def test_push_rejects_unencrypted_files_without_transferring_the_run(self):
        now = self.now - timedelta(hours=1)
        mixed = self.add_run("mariadb", now)
        (mixed / "old-dump.sql.gz").write_bytes(b"legacy fixture")
        mislabeled = self.add_run("mongodb", now)
        (mislabeled / "dump.sql.gz.age").write_bytes(b"plain fixture with an age suffix")

        report, problems = offsite.push(offsite.load_config(self.config_path), self.sftp(), self.now)

        self.assertEqual(len(problems), 2)
        self.assertEqual(report, {})
        self.assertEqual(self.remote("mariadb"), [])
        self.assertEqual(self.remote("mongodb"), [])
        self.assertTrue(mixed.exists())
        self.assertTrue(mislabeled.exists())
        self.assertFalse(any(line.startswith("put ") for line in self.log.read_text().splitlines()))

    def test_a_rejected_run_does_not_hold_back_the_other_runs_of_its_store(self):
        bad = self.add_run("mariadb", self.now - timedelta(days=2))
        (bad / "old-dump.sql.gz").write_bytes(b"legacy fixture")
        good = self.add_run("mariadb", self.now - timedelta(hours=1))

        report, problems = offsite.push(offsite.load_config(self.config_path), self.sftp(), self.now)

        self.assertEqual(len(problems), 1)
        self.assertIn(bad.name, problems[0])
        self.assertEqual(self.remote("mariadb"), [good.name])
        # Kept out of the report, so the reader never treats the store as a clean copy.
        self.assertNotIn("mariadb", report)
        self.assertTrue(bad.exists())

    def test_restore_keeps_matching_mariadb_receipt_as_metadata(self):
        run = self.add_run("mariadb", self.now - timedelta(hours=1))
        file = run / "dump.sql.gz.age"
        receipt = {"version": 1, "run": run.name, "archives": {
            file.name: hashlib.sha256(file.read_bytes()).hexdigest(),
        }}
        (run / "backup-receipt.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")

        _, problems = offsite.push(offsite.load_config(self.config_path), self.sftp(), self.now)
        self.assertEqual(problems, [])
        fetched = self.root / "fetched"
        name, archives = restore.fetch_newest(self.sftp(), "oriso-dev", "mariadb", fetched)

        self.assertEqual(name, run.name)
        self.assertEqual([path.name for path in archives], [file.name])
        self.assertEqual(json.loads((fetched / "backup-receipt.json").read_text()), receipt)

    def test_push_rejects_a_mismatched_mariadb_receipt_before_transfer(self):
        run = self.add_run("mariadb", self.now - timedelta(hours=1))
        (run / "backup-receipt.json").write_text(json.dumps({
            "version": 1, "run": run.name, "archives": {"dump.sql.gz.age": "0" * 64},
        }, sort_keys=True) + "\n")

        _, problems = offsite.push(offsite.load_config(self.config_path), self.sftp(), self.now)

        self.assertEqual(len(problems), 1)
        self.assertEqual(self.remote("mariadb"), [])
        self.assertTrue(run.exists())

    def test_restore_rejects_a_duplicate_checksum_entry(self):
        run = self.add_run("mongodb", self.now - timedelta(hours=1))
        _, problems = offsite.push(offsite.load_config(self.config_path), self.sftp(), self.now)
        self.assertEqual(problems, [])
        receipt = self.box / "oriso-dev" / "mongodb" / run.name / "SHA256SUMS"
        receipt.write_text(receipt.read_text() * 2)

        with self.assertRaisesRegex(RuntimeError, "duplicate archive entry in SHA256SUMS"):
            restore.fetch_newest(self.sftp(), "oriso-dev", "mongodb", self.root / "fetched")
        gets = [line for line in self.log.read_text().splitlines() if line.startswith("get ")]
        self.assertEqual(len(gets), 1)
        self.assertIn("/SHA256SUMS ", gets[0])


if __name__ == "__main__":
    unittest.main()
