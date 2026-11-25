# Nightly MariaDB backup on the Dev master

The Dev master runs a nightly logical backup of the Helm MariaDB StatefulSet with [`backup_mariadb.py`](../scripts/backup_mariadb.py). The script checks that `pod/mariadb-0` is ready before it reads anything. It writes one compressed dump per application schema into a private staging folder and publishes that folder only after every schema succeeded. A failed run leaves no folder behind.

This replaces the hand-installed `/root/db-backups/backup-mariadb.sh` cron job, which is not in Git. That job pinned the pod name `oriso-mariadb-0` and called `mysql`/`mysqldump`. The chart names the pod `mariadb-0`, and the MariaDB 11.0.6 image ships only `mariadb`/`mariadb-dump`. Every run from 14 July to 21 November 2026 therefore failed and left an empty dated folder. `backup_mariadb.py` uses `pod/mariadb-0` and the `mariadb`/`mariadb-dump` clients. `tests/test_mariadb_host_backup_units.py` fails the chart CI if the script's pod name and the rendered StatefulSet drift apart again.

On 21 November 2026 the legacy script was repaired in place as an interim fix (copy of the old version: `/root/db-backups/backup-mariadb.sh.bak-20261121`). Its first good run is `/root/db-backups/mariadb/20261121_070458`. The last complete July nightlies (`20260709`–`20260713`) were copied to `/root/db-backups/archive-nightly-2026-07`, so the legacy job's keep-latest-5 clean-up cannot delete them. Keep that archive folder until the retention decision under #282 says otherwise.

The legacy job and this timer both run at 02:00 UTC. Run only one of them: retire the legacy cron line in the same step as enabling the timer, as shown below.

Installing or changing this on Dev is an operator change on the master. Do it only with explicit approval for that task. Off-site storage, encryption key custody, retention and restore cadence stay under [Helm #282](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/282) and [Helm #533](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/533).

## Install

Run on the Dev master as root, from a checkout of the reviewed `dev` commit.

```bash
set -euo pipefail
install -d -m 0700 /opt/oriso/mariadb-backup /root/db-backups/mariadb-helm
install -m 0600 scripts/backup_mariadb.py /opt/oriso/mariadb-backup/backup_mariadb.py
install -m 0644 runbooks/systemd/oriso-mariadb-backup.service runbooks/systemd/oriso-mariadb-backup.timer /etc/systemd/system/
systemd-analyze verify /etc/systemd/system/oriso-mariadb-backup.service /etc/systemd/system/oriso-mariadb-backup.timer
systemctl daemon-reload

# Read-only preflight: confirms the ready pod without opening MariaDB.
python3 -I /opt/oriso/mariadb-backup/backup_mariadb.py /etc/rancher/k3s/k3s.yaml default caritas --check-target
```

Run one backup by hand, then check that a complete folder appeared:

```bash
systemctl start oriso-mariadb-backup.service
systemctl show oriso-mariadb-backup.service -p Result -p ExecMainStatus
ls -l "$(ls -1d /root/db-backups/mariadb-helm/mariadb-* | tail -1)"
```

Every application schema must have a non-empty `.sql.gz` file.

### Switch schedules

Run this block after the manual backup has succeeded. It expects one legacy entry and an inactive, disabled replacement timer. Keep the printed snapshot path for rollback.

```bash
(
  set -euo pipefail
  umask 077
  timer=oriso-mariadb-backup.timer
  if [[ "$(systemctl show "$timer" -p ActiveState --value)" != inactive ||
        "$(systemctl show "$timer" -p UnitFileState --value)" != disabled ]]; then
    printf 'Check the existing timer state before switching schedules.\n' >&2
    exit 1
  fi

  cutover_dir=$(mktemp -d /root/db-backups/mariadb-scheduler-cutover.XXXXXX)
  crontab -l > "$cutover_dir/before"
  if ! awk '
    $1 == "0" && $2 == "2" && $3 == "*" && $4 == "*" && $5 == "*" &&
      $6 == "/root/db-backups/backup-mariadb.sh" { removed++; next }
    { print }
    END { if (removed != 1) exit 1 }
  ' "$cutover_dir/before" > "$cutover_dir/after"; then
    printf 'Expected exactly one 02:00 legacy backup entry. Check %s/before.\n' "$cutover_dir" >&2
    exit 1
  fi

  rollback() {
    local status=$?
    trap - EXIT
    if ! systemctl disable --now "$timer"; then
      printf 'Timer cleanup failed. Check both schedules before retrying.\n' >&2
    fi
    if crontab "$cutover_dir/before"; then
      printf 'Restored the original cron schedule from %s/before.\n' "$cutover_dir" >&2
    else
      printf 'Cron restore failed. Original schedule saved in %s/before.\n' "$cutover_dir" >&2
    fi
    exit "$status"
  }

  trap rollback EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  crontab "$cutover_dir/after"
  systemctl enable --now "$timer"
  systemctl is-active --quiet "$timer"
  trap - EXIT HUP INT TERM

  printf 'Original cron schedule saved in %s/before.\n' "$cutover_dir"
  systemctl list-timers "$timer" --no-pager
)
```

Keep the existing dumps in `/root/db-backups`, including `mariadb/` from the legacy job and `archive-nightly-2026-07/`, until a restore from the new folder has been verified with [the isolated restore runbook](mariadb-isolated-restore.md).

## Check the next morning

```bash
systemctl show oriso-mariadb-backup.service -p Result -p ExecMainExitTimestamp
journalctl -u oriso-mariadb-backup.service --since yesterday --no-pager | tail -20
```

`Result=success` and a new `mariadb-*` folder dated today mean the run is complete. On failure, the journal names the step: the target check, schema discovery, or a named schema dump.

## Encryption

Once the recovery identity custody under #282 is agreed, install `age` and a recipients file on the master. Then switch the service to encrypted output with a drop-in, so dumps never land in plain text:

```ini
# /etc/systemd/system/oriso-mariadb-backup.service.d/encrypt.conf
[Service]
ExecStart=
ExecStart=/usr/bin/python3 -I /opt/oriso/mariadb-backup/backup_mariadb.py /etc/rancher/k3s/k3s.yaml default caritas /root/db-backups/mariadb-helm --age-recipients-file /etc/oriso/mariadb-backup.age-recipients
```

## Not covered here

- Off-site copy, 30 daily and 12 weekly retention and the monthly restore test (#533, #282): a separate change.
- MongoDB and Matrix PostgreSQL: [datastore host backup](datastore-host-backup.md).
