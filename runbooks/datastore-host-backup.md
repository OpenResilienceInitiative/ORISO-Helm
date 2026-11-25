# Nightly encrypted MongoDB and Synapse PostgreSQL backup on the Dev master

This adds the two datastores that had no backup job. [`backup_datastores.py`](../scripts/backup_datastores.py) dumps one pod, streams the dump through gzip and `age`, and publishes the run folder by a single rename. A failed run leaves no folder behind. It follows the rules of the [MariaDB writer](mariadb-host-backup.md) and reuses its encryption code. Copying to the Storage Box, retention and the monthly restore test follow in a separate change (#533, #282).

| Datastore | Pod | Local folder | File in each run |
| --- | --- | --- | --- |
| MongoDB 5.0 | `pod/mongodb-0` | `/root/db-backups/mongodb-helm` | `mongodb.archive.gz.age` (`mongodump --archive`) |
| Synapse PostgreSQL 15 | `pod/matrix-postgres-0` | `/root/db-backups/matrix-postgres-helm` | `synapse.sql.gz.age` (`pg_dump`, plain SQL) |

The backup refuses to run without a recipients file, so a plain-text dump can never be written.

## Who can decrypt

The host holds only **public** age recipients. Create two identities, one per key holder, each on that person's own machine:

```bash
age-keygen -o ~/oriso-backup-identity.txt   # keep the file in your password manager, never on a server
age-keygen -y ~/oriso-backup-identity.txt   # prints the public recipient to hand over
```

Put both public recipients, one per line, into `/etc/oriso/backup.age-recipients` on the master (mode 0600). Every archive is then readable by either holder alone, and losing one identity does not lose the backups. Use the same file for the MariaDB drop-in. Never store an identity on the master, in Git, in Slack or in the cluster.

## Install on Dev

This is an operator change on the master. Do it only with explicit approval for that task, from a checkout of the reviewed `dev` commit, as root.

```bash
set -euo pipefail
command -v age >/dev/null || apt-get install -y age
install -d -m 0700 /opt/oriso/datastore-backup /root/db-backups/mongodb-helm /root/db-backups/matrix-postgres-helm /etc/oriso
install -m 0600 scripts/backup_mariadb.py scripts/backup_datastores.py /opt/oriso/datastore-backup/
for ds in mongodb matrix-postgres; do
  install -m 0644 runbooks/systemd/oriso-$ds-backup.service runbooks/systemd/oriso-$ds-backup.timer /etc/systemd/system/
done
systemd-analyze verify /etc/systemd/system/oriso-mongodb-backup.service /etc/systemd/system/oriso-matrix-postgres-backup.service
systemctl daemon-reload
for ds in mongodb matrix-postgres; do
  python3 -I /opt/oriso/datastore-backup/backup_datastores.py /etc/rancher/k3s/k3s.yaml default caritas "$ds" --check-target
  systemctl start oriso-$ds-backup.service
  systemctl show oriso-$ds-backup.service -p Result
  ls -l "$(ls -1d /root/db-backups/$ds-helm/$ds-* | tail -1)"
done
```

`Result=success` and one non-empty `.age` file per run mean the backup works. Then enable the timers:

```bash
systemctl enable --now oriso-mongodb-backup.timer oriso-matrix-postgres-backup.timer
systemctl list-timers 'oriso-*-backup.timer' --no-pager
```

The scripts stay in `/opt/oriso/datastore-backup/` with `backup_mariadb.py` beside them, because `backup_datastores.py` loads it.

## Restore

Restore needs an age identity, so it never runs on the master. By hand, on a machine that holds an identity, in bash with `pipefail` so a wrong key or a damaged archive fails the whole pipeline instead of restoring nothing and reporting success:

```bash
set -o pipefail
age --decrypt -i ~/oriso-backup-identity.txt mongodb.archive.gz.age | gunzip | mongorestore --archive --drop --uri "$SCRATCH_MONGO_URI" || echo "MongoDB restore FAILED"
age --decrypt -i ~/oriso-backup-identity.txt synapse.sql.gz.age | gunzip | psql -X --set ON_ERROR_STOP=on "$SCRATCH_POSTGRES_URI" || echo "PostgreSQL restore FAILED"
```

Restore into a fresh scratch instance with no mail, webhook or application access.

The scheduled Synapse dump keeps the one-time-key table schema and leaves its
rows out of the archive. Synapse clients upload fresh one-time keys after
recovery. See the [Synapse backup guidance](https://element-hq.github.io/synapse/latest/usage/administration/backups.html#synapse-specfic-details).

Older dumps can contain one-time keys that were already used. After a
successful PostgreSQL restore and before starting Synapse, clear those rows
and verify the count in the isolated destination database:

```bash
: "${SCRATCH_POSTGRES_URI:?Set the isolated PostgreSQL destination}"
psql -X --set ON_ERROR_STOP=on "$SCRATCH_POSTGRES_URI" \
  -c 'TRUNCATE TABLE public.e2e_one_time_keys_json; SELECT count(*) AS one_time_keys FROM public.e2e_one_time_keys_json;'
```

Proceed only after the command succeeds and reports zero rows. The isolated
off-site restore tester performs this cleanup in its disposable PostgreSQL
container before reporting a successful restore.

## Notes

- `mongodump` has no environment variable for its password. The script writes it into a private temporary `--config` file inside the MongoDB container (mode 0600) and removes the file on exit, so the password never appears in a process list.
- The dump is a logical backup taken while the pod runs. MongoDB `--archive` is not a point-in-time snapshot across collections. PostgreSQL's `pg_dump` is consistent.
