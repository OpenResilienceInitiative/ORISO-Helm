# Off-site backups for Dev: Storage Box copy, retention and monthly restore test

[Helm #533](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/533) and [#282](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/282), decision of 21 November 2026.

| Decision | Value |
| --- | --- |
| Target | the existing Hetzner Storage Box (EU), in its own sub-account and folder for Dev |
| Scope | MariaDB, MongoDB, Synapse PostgreSQL |
| Schedule | dumps 02:00–02:40 UTC, copy 03:00 UTC, freshness check 08:00 UTC |
| Retention | 30 daily and 12 weekly runs on the box |
| Encryption | age, two key holders, private keys outside the cluster |
| Restore test | monthly, by a key holder, with the script below |

## How it fits together

1. The dump jobs ([MariaDB](mariadb-host-backup.md), [MongoDB and PostgreSQL](datastore-host-backup.md)) write age-encrypted runs under `/root/db-backups/<datastore>-helm`.
2. [`offsite_backup.py push`](../scripts/offsite_backup.py) copies every complete run it does not find on the box. It uploads into a hidden `.partial` folder, checks every file size, writes a `SHA256SUMS` receipt, and only then renames the folder. A broken transfer never looks like a finished run.
3. After a successful copy it prunes the box to the newest run of each of the last 30 days plus the newest run of each of the last 12 ISO weeks. It refuses to delete more than 60 runs in one go, and it ignores folders with an invalid calendar date or a different datastore prefix. The copy, freshness check and restore use the same datastore boundary.
4. Local runs older than 14 days are removed once they are on the box or outside the retention ladder. A run the box does not hold yet is never removed.
5. [`offsite_backup.py check`](../scripts/offsite_backup.py) fails when a datastore has no run, the newest run is older than 36 hours, or it lacks its receipt or an `.age` file. The unit's result and the archive evidence feed the [backup monitoring checks](#monitor-the-backup-jobs).

Only encrypted database dumps and their checksum metadata leave the master.
Before any upload, the copy checks every payload's filename and binary age
format header. A selected run with a mixed or mislabeled payload is not copied
and stays local for diagnosis. The copy continues with the store's other runs
and its pruning, but exits 1 and names the rejected run every night until it is
gone. After diagnosis, move the run out of the store's backup folder on the
host, then start `oriso-offsite-backup.service` and check that
`/var/lib/oriso-offsite-backup/push.json` reports `"ok": true`. MariaDB can also carry the writer's
optional `backup-receipt.json` when it matches the run and encrypted archive
checksums. See the [age file format](https://age-encryption.org/v1).

Key holders retain the age recovery identities outside the cluster. The
monthly restore verifies the full checksum inventory and authenticates the
encrypted data when it decrypts each archive.

## Storage Box access

The Storage Box `storage-box-1` (BX11, Falkenstein) already serves the Dreambau server. Its credentials are on that server and in Hetzner's console, not in this chart, and the Dev master has none. Dev gets its own access so that a Dev compromise cannot touch Dreambau's backups:

1. In the Hetzner console, create a **sub-account** of the Storage Box with its own home folder (for example `oriso-dev`), SSH enabled and external reachability on. Do not reuse the main account or the Dreambau key.
2. On the Dev master, create a dedicated key and register its public half for the sub-account:

   ```bash
   ssh-keygen -t ed25519 -N '' -f /root/.ssh/oriso-dev-storagebox_ed25519
   ```

   Add `/root/.ssh/oriso-dev-storagebox_ed25519.pub` to the sub-account's authorized keys in the Hetzner console.
3. Record the box's host key once, over a channel you trust, and compare its fingerprint with the one in the Hetzner console:

   ```bash
   ssh-keyscan -p 23 uXXXXXX.your-storagebox.de > /root/.ssh/oriso-dev-storagebox.known_hosts
   ssh-keygen -lf /root/.ssh/oriso-dev-storagebox.known_hosts
   ```

4. Copy [`offsite-backup.example.json`](offsite-backup.example.json) to `/etc/oriso/offsite-backup.json` (mode 0600), and fill in host, user and the file paths. The file holds no password; authentication is the key.

## Install on Dev

This is an operator change on the master. Do it only with explicit approval for that task, from a checkout of the reviewed `dev` commit, as root, after the dump jobs have produced at least one run.

```bash
set -euo pipefail
install -d -m 0700 /opt/oriso/offsite-backup
install -m 0600 scripts/offsite_backup.py /opt/oriso/offsite-backup/offsite_backup.py
for unit in oriso-offsite-backup oriso-offsite-check; do
  install -m 0644 runbooks/systemd/$unit.service runbooks/systemd/$unit.timer /etc/systemd/system/
done
systemd-analyze verify /etc/systemd/system/oriso-offsite-backup.service /etc/systemd/system/oriso-offsite-check.service
systemctl daemon-reload
systemctl start oriso-offsite-backup.service
systemctl show oriso-offsite-backup.service -p Result
cat /var/lib/oriso-offsite-backup/push.json
systemctl start oriso-offsite-check.service && cat /var/lib/oriso-offsite-backup/check.json
```

Both runs must end with `"ok": true`. Then enable the timers:

```bash
systemctl enable --now oriso-offsite-backup.timer oriso-offsite-check.timer
systemctl list-timers 'oriso-offsite-*' --no-pager
```

## Monthly restore test

**Who and when:** one of the two key holders, in the first week of every month. It runs on that person's machine (Docker and `age` installed), never on the Dev master, because it needs the private key.

1. Put a read-only copy of the box settings on your machine: an `offsite-backup.json` like the example, with an SSH key that may read the sub-account. Ask the operator for it; do not copy the Dev master's key.
2. Run it with your own identity file:

   ```bash
   python3 scripts/offsite_restore_test.py \
     --config ~/oriso-offsite-backup.json \
     --age-identity-file ~/oriso-backup-identity.txt \
     --report ~/restore-test-$(date +%F).md
   ```

3. The script downloads the newest run of each datastore, checks the `SHA256SUMS` receipt, decrypts with `age`, and restores into throwaway containers with the versions that run on Dev (`mariadb:11.0.6`, `mongo:5.0`, `postgres:15-alpine`). The containers have no network. It checks that every MariaDB schema in the run has tables, that MongoDB has collections with documents, and that PostgreSQL has public tables. It removes containers and scratch files, prints a Markdown report and exits 1 on any failure.
4. Paste the report into [#533](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/533) as a comment. A failed test is a P1 for the next working day: run the dump and copy jobs by hand and check the journal.

Rehearsals with synthetic data add `--marker mariadb=schema.table --marker mongodb=db.collection --marker matrix-postgres=table`, which makes the test fail unless that marker holds rows.

## Monitor the backup jobs

The existing [journal reader](signoz-dev.md#read-completed-checks-for-the-incident-consumer)
has a separate scope for the copy and verification services. Give the backup
scope its own cursor.

The two status receipts are root-only (`0600` in the `0700` state directory),
so an unprivileged reader gets `unreadable-probe`. On the approved backup host,
install the reader next to the copy script as a root-owned file and let the
reader account run only that script as root. For example, in
`/etc/sudoers.d/oriso-offsite-reader` (edit with `visudo -f`):

```text
oriso-reader ALL=(root) NOPASSWD: /usr/bin/python3 -I /opt/oriso/offsite-backup/signoz_check_journal.py --scope offsite-backup *
```

Every mode of the reader only reads the journal, systemd state and the
receipts. Then poll with:

```sh
sudo -n /usr/bin/python3 -I /opt/oriso/offsite-backup/signoz_check_journal.py --scope offsite-backup --since '26 hours ago'
sudo -n /usr/bin/python3 -I /opt/oriso/offsite-backup/signoz_check_journal.py --scope offsite-backup --cursor "$last_backup_cursor"
```

The completion page follows the existing machine, boot, unit and invocation
deduplication rules. A backup script exit status of 1 is `CRITICAL`. An invalid
invocation can exit with status 2 and is `UNKNOWN`, as are timeouts, failed
starts and incomplete systemd evidence. For `UNKNOWN`, inspect the unit
invocation and journal first. A successful journal read can return an empty
completion page. Evaluate its freshness snapshot on every poll.

The snapshot reports the verification probe and each of `mariadb`, `mongodb`
and `matrix-postgres` separately. The archive clock uses the newest known
complete archive for that store. An archive older than 26 hours is
`CRITICAL`, as is an explicitly incomplete archive. Missing or invalid
evidence stays `UNKNOWN`. The verification service still uses its configured
36-hour default.

A verified upload after the last check can refresh archive evidence before the
next daily verification. A copy with zero uploads for a store carries forward
that store's verified upload and original verification time when the previous
copy receipt contains them. A newer verification supersedes that upload evidence.

A failed copy can omit a store from its report. Its prior verified upload stays
under `carriedUploads` for recovery. The failed run and its reported problems
remain visible. A later successful copy that uploads nothing for this store
restores that receipt with its original verification time. If evidence remains
stale or missing after recovery, start `oriso-offsite-check.service` on the
approved backup host and inspect the new check receipt.

Before enabling incident delivery, verify the restricted read command,
cursor replay, a simulated job failure, a stale archive and one recovery
through the approved consumer. The reader exposes fixed states, reasons and
timestamps. Keep the raw receipts and journal on the host for diagnosis.

## Losing a key

Either holder's identity restores every archive. If one holder leaves or loses their identity, the other generates a replacement, the recipients file is updated, and new runs are encrypted for both current holders. **Existing archives stay readable only by the identities they were encrypted for**, so keep a departing holder's identity until the last archive made for it has aged out (12 weeks), or re-encrypt the archives you still need.

## Not covered

- Server image backups and the Dreambau backups, which have their own jobs.
- Staging and production. They need their own sub-accounts and a separate approval.
- Incident delivery and per-store archive size bands need operator acceptance
  with the scheduled backup and restore evidence.
