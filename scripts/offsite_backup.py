#!/usr/bin/env python3
"""Copy encrypted Dev backup runs to the Hetzner Storage Box, keep 30 daily and 12 weekly runs, check freshness.

The host sends encrypted archives and their checksum metadata. The age recovery identity stays off the host.
Credentials and the host's SSH identity live in a root-only JSON file outside Git (see the runbook).
"""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

RUN_NAME = re.compile(r"^(?P<prefix>[a-z][a-z0-9-]*)-(?P<stamp>\d{8}T\d{6}Z)(?:-[A-Za-z0-9_]+)?$")
SAFE_PATH = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")
LS_LONG = re.compile(r"^\S+\s+(?:\d+|\?)\s+\S+\s+\S+\s+(?P<size>\d+)\s+\w+\s+\d+\s+[\d:]+\s+(?P<name>.+)$")
SFTP_TIMEOUT_SECONDS = 1800
MAX_DELETIONS_PER_RUN = 60
MAX_STATUS_BYTES = 16 * 1024
UTC_ISO = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)"
)
RECEIPT = "SHA256SUMS"
MARIADB_RECEIPT = "backup-receipt.json"
AGE_FORMAT_LINE = b"age-encryption.org/v1\n"
ARCHIVE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.$-]*\.age")


class OffsiteError(RuntimeError):
    pass


def run_stamp(name, *, prefix=None):
    match = RUN_NAME.fullmatch(name)
    if not match or (prefix is not None and match["prefix"] != prefix):
        return None
    try:
        return datetime.strptime(match["stamp"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def select_keep(runs, daily, weekly):
    """Return the run names to keep: the newest run of each of the latest `daily` days plus the
    newest run of each of the latest `weekly` ISO weeks. `runs` is a list of (name, datetime)."""
    ordered = sorted(runs, key=lambda run: run[1], reverse=True)
    days, weeks = {}, {}
    for name, when in ordered:
        days.setdefault(when.date(), name)
        weeks.setdefault(tuple(when.isocalendar())[:2], name)
    keep = {days[key] for key in sorted(days, reverse=True)[:daily]}
    keep |= {weeks[key] for key in sorted(weeks, reverse=True)[:weekly]}
    return keep


def load_config(path):
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise OffsiteError("Could not read the off-site backup configuration") from error
    box = config.get("storageBox", {})
    for key in ("host", "user", "identityFile", "knownHostsFile", "remoteBase"):
        if not isinstance(box.get(key), str) or not box[key].strip():
            raise OffsiteError(f"Configuration is missing storageBox.{key}")
    if not SAFE_PATH.fullmatch(box["remoteBase"]) or ".." in box["remoteBase"].split("/"):
        raise OffsiteError("storageBox.remoteBase must be a plain relative path")
    if not config.get("sources"):
        raise OffsiteError("Configuration lists no backup sources")
    for source in config["sources"]:
        if not re.fullmatch(r"[a-z][a-z0-9-]*", source.get("prefix", "")) or not source.get("root"):
            raise OffsiteError("Every source needs a lowercase prefix and a root folder")
    retention = config.setdefault("retention", {})
    retention.setdefault("daily", 30)
    retention.setdefault("weekly", 12)
    retention.setdefault("maxAgeHours", 36)
    retention.setdefault("localKeepDays", 14)
    if min(retention["daily"], retention["weekly"], retention["maxAgeHours"]) < 1:
        raise OffsiteError("Retention values must be positive")
    return config


class Sftp:
    """Thin wrapper over the OpenSSH sftp client in batch mode; no mounts, no shell on the box."""

    def __init__(self, box, runner=subprocess.run):
        self.runner = runner
        self.command = [
            shutil.which("sftp") or "sftp", "-b", "-", "-P", str(box.get("port", 23)),
            "-i", box["identityFile"], "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={box['knownHostsFile']}",
            "-o", "ConnectTimeout=30", f"{box['user']}@{box['host']}",
        ]

    def batch(self, lines):
        result = self.runner(
            self.command, input="\n".join(lines) + "\n", text=True, capture_output=True,
            timeout=SFTP_TIMEOUT_SECONDS,
        )
        if result.returncode:
            raise OffsiteError(f"sftp batch failed with status {result.returncode}")
        return result.stdout

    def names(self, directory):
        try:
            out = self.batch([f"ls -1 {directory}"])
        except OffsiteError:
            return None
        return sorted({line.strip().rsplit("/", 1)[-1] for line in out.splitlines()
                       if line.strip() and not line.startswith("sftp>")})

    def sizes(self, directory):
        out = self.batch([f"ls -l {directory}"])
        return {m["name"].strip().rsplit("/", 1)[-1]: int(m["size"]) for line in out.splitlines()
                if (m := LS_LONG.match(line.strip()))}


def local_runs(source):
    root = Path(source["root"])
    if not root.is_dir():
        raise OffsiteError(f"Backup folder for {source['prefix']} does not exist")
    runs = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and not entry.is_symlink() and run_stamp(entry.name, prefix=source["prefix"]):
            files = sorted(p for p in entry.iterdir() if p.is_file() and p.name != RECEIPT)
            if files and all(p.stat().st_size > 0 for p in files):
                runs.append((entry, files))
    return runs


def archive_names(prefix, names):
    """Return the encrypted archive names, allowing the MariaDB writer's optional inventory."""
    archives = set(names)
    if prefix == "mariadb":
        archives.discard(MARIADB_RECEIPT)
    if not archives or any(not ARCHIVE_NAME.fullmatch(name) for name in archives):
        raise OffsiteError("Backup run contains an unsupported file or has no encrypted archives")
    return archives


def write_receipt(run, files, prefix):
    paths = {path.name: path for path in files}
    if any(path.is_symlink() for path in paths.values()):
        raise OffsiteError("Backup run contains a symbolic link")
    digests = {}
    for name in sorted(archive_names(prefix, paths)):
        digest = hashlib.sha256()
        with paths[name].open("rb") as handle:
            header = handle.read(len(AGE_FORMAT_LINE))
            if header != AGE_FORMAT_LINE:
                raise OffsiteError("Backup archive has an invalid binary age format header")
            digest.update(header)
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        digests[name] = digest.hexdigest()
    if MARIADB_RECEIPT in paths:
        # The writer's canonical receipt contains only the run name and these ciphertext hashes.
        inventory = {"version": 1, "run": run.name, "archives": digests}
        expected = (json.dumps(inventory, sort_keys=True) + "\n").encode("utf-8")
        with paths[MARIADB_RECEIPT].open("rb") as metadata:
            if metadata.read(len(expected) + 1) != expected:
                raise OffsiteError("MariaDB backup receipt does not match the encrypted archives")
        digests[MARIADB_RECEIPT] = hashlib.sha256(expected).hexdigest()
    receipt = run / RECEIPT
    if receipt.is_symlink():
        raise OffsiteError("Backup checksum receipt is a symbolic link")
    receipt.write_text("".join(f"{digests[name]}  {name}\n" for name in sorted(digests)), encoding="utf-8")
    receipt.chmod(0o600)
    return receipt


def upload_run(sftp, base, prefix, run, files):
    """Upload into a hidden partial folder, verify every size, then rename to the final name."""
    final = f"{base}/{prefix}/{run.name}"
    partial = f"{base}/{prefix}/.{run.name}.partial"
    receipt = write_receipt(run, files, prefix)
    sources = files + [receipt]
    lines = [f"-mkdir {part}" for part in (base, f"{base}/{prefix}")]
    lines += [f"-rm {partial}/{p.name}" for p in sources] + [f"-rmdir {partial}", f"mkdir {partial}"]
    lines += [f'put "{p}" {partial}/{p.name}' for p in sources]
    sftp.batch(lines)
    remote = sftp.sizes(partial)
    for path in sources:
        if remote.get(path.name) != path.stat().st_size:
            raise OffsiteError(f"Size check failed for {prefix}/{run.name}/{path.name}")
    sftp.batch([f"rename {partial} {final}"])


def prune_remote(sftp, base, prefix, retention, deletions_left):
    names = sftp.names(f"{base}/{prefix}")
    runs = [(n, w) for n in (names or []) if (w := run_stamp(n, prefix=prefix))]
    if not runs:
        return 0
    keep = select_keep(runs, retention["daily"], retention["weekly"])
    doomed = [n for n, _ in sorted(runs, key=lambda r: r[1]) if n not in keep]
    if len(doomed) > deletions_left:
        raise OffsiteError(f"Refusing to delete {len(doomed)} runs of {prefix} at once")
    for name in doomed:
        files = sftp.sizes(f"{base}/{prefix}/{name}")
        sftp.batch([f"rm {base}/{prefix}/{name}/{f}" for f in files] + [f"rmdir {base}/{prefix}/{name}"])
    return len(doomed)


def prune_local(source, protected, keep_days, now):
    """Remove local runs older than keep_days that are off-site or outside the retention ladder."""
    removed = 0
    for run, _files in local_runs(source):
        if now - run_stamp(run.name) > timedelta(days=keep_days) and run.name not in protected:
            shutil.rmtree(run)
            removed += 1
    return removed


def push(config, sftp, now=None):
    now = now or datetime.now(timezone.utc)
    box, retention = config["storageBox"], config["retention"]
    base, report, failures = box["remoteBase"].rstrip("/"), {}, []
    deletions = MAX_DELETIONS_PER_RUN
    for source in config["sources"]:
        prefix = source["prefix"]
        try:
            remote = set(sftp.names(f"{base}/{prefix}") or [])
            local = local_runs(source)
            names = remote | {run.name for run, _ in local}
            candidates = [(n, w) for n in names if (w := run_stamp(n, prefix=prefix))]
            ladder = select_keep(candidates, retention["daily"], retention["weekly"])
            uploaded, rejected = 0, False
            verified_archive_at = None
            for run, files in local:
                if run.name in ladder and run.name not in remote:
                    try:
                        upload_run(sftp, base, prefix, run, files)
                    except OffsiteError as error:
                        # One bad run must not hold back the store's other runs or its pruning.
                        failures.append(f"{prefix}: {run.name}: {error}")
                        rejected = True
                        continue
                    remote.add(run.name)
                    uploaded += 1
                    archive_at = run_stamp(run.name, prefix=prefix)
                    if (archive_at is not None and archive_at <= now
                            and any(path.name.endswith(".age") for path in files)):
                        verified_archive_at = max(verified_archive_at or archive_at, archive_at)
            pruned = prune_remote(sftp, base, prefix, retention, deletions)
            deletions -= pruned
            # Only a run the ladder wants but the box does not hold yet is protected from local clean-up.
            local_removed = prune_local(source, ladder - remote, retention["localKeepDays"], now)
            if rejected:
                # Leave the store out so its receipt is carried, never read as a clean copy.
                continue
            report[prefix] ={"uploaded": uploaded, "prunedRemote": pruned, "prunedLocal": local_removed}
            if verified_archive_at is not None:
                report[prefix]["archiveAt"] = verified_archive_at.isoformat(timespec="seconds")
                report[prefix]["complete"] = True
        except OffsiteError as error:
            failures.append(f"{prefix}: {error}")
    return report, failures


def check(config, sftp, now=None):
    now = now or datetime.now(timezone.utc)
    base, limit = config["storageBox"]["remoteBase"].rstrip("/"), timedelta(hours=config["retention"]["maxAgeHours"])
    problems, newest = [], {}
    for source in config["sources"]:
        prefix = source["prefix"]
        runs = [
            (name, archive_at) for name in sftp.names(f"{base}/{prefix}") or []
            if (archive_at := run_stamp(name, prefix=prefix)) is not None and archive_at <= now
        ]
        if not runs:
            problems.append(f"{prefix}: no off-site run")
            continue
        name, when = max(runs, key=lambda r: r[1])
        files = sftp.sizes(f"{base}/{prefix}/{name}")
        complete = RECEIPT in files and any(file.endswith(".age") for file in files) and 0 not in files.values()
        newest[prefix] = {
            "run": name,
            "ageHours": round((now - when).total_seconds() / 3600, 1),
            "archiveAt": when.isoformat(timespec="seconds"),
            "complete": complete,
        }
        if now - when > limit:
            problems.append(f"{prefix}: newest off-site run {name} is older than {limit}")
        if not complete:
            problems.append(f"{prefix}: newest off-site run {name} is incomplete")
    return newest, problems


def unique_status_object(fields):
    result = dict(fields)
    if len(result) != len(fields):
        raise ValueError("duplicate status fields")
    return result


def status_timestamp(value):
    if not isinstance(value, str) or not UTC_ISO.fullmatch(value):
        raise ValueError("expected UTC ISO timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def preserve_verified_uploads(path, status):
    """Keep verified receipts across interrupted runs without changing their timestamps."""
    try:
        with path.open("rb") as previous_file:
            data = previous_file.read(MAX_STATUS_BYTES + 1)
        if len(data) > MAX_STATUS_BYTES:
            return
        previous = json.loads(data, object_pairs_hook=unique_status_object)
        if (
            previous["command"] != "push" or type(previous["ok"]) is not bool
            or not isinstance(previous["report"], dict)
        ):
            return
        previous_at = status_timestamp(previous["at"])
        if previous_at > status_timestamp(status["at"]):
            return
    except (OSError, KeyError, TypeError, ValueError, RecursionError):
        return

    carried = previous.get("carriedUploads", {})
    if not isinstance(carried, dict):
        carried = {}
    previous_uploads = {**carried, **previous["report"]}
    carry_forward = {}
    for prefix, old in previous_uploads.items():
        entry = status["report"].get(prefix)
        if (
            (entry is not None and entry["uploaded"] != 0)
            or (entry is None and status["ok"])
            or not isinstance(old, dict) or old.get("complete") is not True
            or type(old.get("uploaded")) is not int or old["uploaded"] < 0
        ):
            continue
        verified = old.get("verifiedAt")
        if "verifiedAt" not in old and old["uploaded"] > 0 and prefix in previous["report"]:
            verified = previous["at"]
        try:
            verified_at = status_timestamp(verified)
            archive_at = status_timestamp(old["archiveAt"])
        except (KeyError, TypeError, ValueError):
            continue
        if not archive_at <= verified_at <= previous_at:
            continue
        receipt = {"archiveAt": old["archiveAt"], "complete": True, "verifiedAt": verified}
        if entry is None:
            # The current run did not report this store. Keep the receipt outside the active
            # report so the reader cannot interpret a failed copy as a successful no-op.
            carry_forward[prefix] = {"uploaded": 0, **receipt}
        else:
            entry.update(receipt)
    if carry_forward:
        status["carriedUploads"] = carry_forward


def write_status_file(path, status):
    if status.get("command") == "push":
        preserve_verified_uploads(path, status)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False,
        ) as output:
            temporary_path = Path(output.name)
            output.write(json.dumps(status, indent=1) + "\n")
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("push", "check"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--status-file", type=Path, help="write a JSON status for the last run")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        config = load_config(args.config)
        sftp = Sftp(config["storageBox"])
        if args.command == "push":
            report, problems = push(config, sftp)
        else:
            report, problems = check(config, sftp)
    except OffsiteError as error:
        report, problems = {}, [str(error)]
    except subprocess.TimeoutExpired:
        report, problems = {}, ["Off-site backup command timed out"]
    except OSError:
        report, problems = {}, ["Could not access required off-site backup resources"]
    status = {"command": args.command, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "ok": not problems, "report": report, "problems": problems}
    if args.status_file:
        write_status_file(args.status_file, status)
    print(json.dumps(status))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
