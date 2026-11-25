# External Dev SigNoz heartbeat receiver

Run the receiver on the approved monitoring host outside the Dev cluster and
its hosting provider. Use the runner's approved production WSGI server and
TLS reverse proxy. Expose only `POST /pulse`, limit requests to 16 KiB and set
request timeouts at the proxy. Forward the Basic authorization header to the
receiver over loopback. Keep that header and the body out of access logs.

## Configure the receiver

Run with Python 3.10 or newer. Use a dedicated service identity and persistent
local storage for SQLite. Give that identity a private writable state directory
and an owner-readable password file from the operator's secret store. Linux
uses the kernel boot ID to identify the monotonic clock across processes.
The development probe also supports the macOS boot session ID.

Set these environment variables for the service.

| Variable | Value |
| --- | --- |
| `SIGNOZ_HEARTBEAT_DB` | Absolute path to the database in the persistent state directory |
| `SIGNOZ_HEARTBEAT_CLUSTER` | Exact Dev collector cluster name |
| `SIGNOZ_HEARTBEAT_PASSWORD_FILE` | Path to the file containing the receiver's Basic Auth password |

From the checkout, the Gunicorn factory can be started as follows.

```sh
gunicorn --chdir scripts --bind 127.0.0.1:9785 --timeout 10 \
  'signoz_heartbeat_receiver:from_environment()'
```

## Operate the Linux services

The systemd units use a local Gunicorn listener and two service accounts.
They require systemd 247 or newer, Python 3.10 or newer and an approved
Gunicorn version with `--no-control-socket` (25.1 or newer). [Unit files](systemd/)

1. Provision system accounts `signoz-heartbeat` with primary group
   `signoz-heartbeat` and `signoz-heartbeat-probe` with its own primary
   group. Use non-login accounts. The check unit uses the receiver group to
   read the database. The receiver has exclusive access to its runtime
   credential.
2. Install the reviewed `scripts/signoz_heartbeat_receiver.py` at
   `/opt/oriso/signoz-heartbeat/signoz_heartbeat_receiver.py`. Install the
   approved Gunicorn dependency in `/opt/oriso/signoz-heartbeat/venv`. Keep
   the code and virtual environment root-owned and readable by the services.
3. Deliver the webhook password from the secret store to
   `/etc/oriso/signoz-heartbeat/webhook-password`, owned by root with mode
   `0400`. The receiver unit loads one private runtime copy. For rotation,
   replace the source file, restart the receiver and update the dedicated
   SigNoz channel within the same maintenance window.
4. Install the three unit files in `/etc/systemd/system`. Run
   `systemd-analyze verify /etc/systemd/system/oriso-signoz-heartbeat*`
   and `systemctl daemon-reload`. Enable the receiver after the approved
   reverse proxy and receiver configuration are ready.

```sh
systemctl enable --now oriso-signoz-heartbeat.service
```

The receiver writes to
`/var/lib/oriso-signoz-heartbeat/heartbeat.sqlite3`. systemd creates the
state directory with owner write and group read/traverse permissions. When
reusing an existing database, ensure the receiver owns it and its group can
read it. Keep the state on durable local storage. Replacing the app or its
units preserves this directory.

Create the dedicated SigNoz webhook channel with the receiver's HTTPS URL,
username `heartbeat`, the matching password and resolved delivery disabled.
Configure TLS verification in SigNoz. The Helm sender is opt-in and requires a
fresh sample from the named cluster collector before provisioning.

The receiver accepts one untruncated firing alert for the exact Dev rule and
cluster. It checks the rule labels and the evaluation timestamp. A repeat can
return HTTP 204 after a lost acknowledgment without extending the deadline.
The receiver commits valid advancing timestamps to SQLite before replying.
A storage failure returns HTTP 503 so the sender can retry. The database holds
the cluster key, evaluation timestamp, boot ID and monotonic receipt time.

## Connect the incident check

Give the monitor identity read access to the same database and set the database
path and cluster name in its environment. Run this local command on the
receiver host. The monitor configuration needs no password file.

```sh
python3 scripts/signoz_heartbeat_receiver.py
```

The command returns one JSON line. Exit 0 means a valid pulse was received
within 600 seconds on the current boot. Exit 2 reports an elapsed deadline,
no pulse, a changed boot ID, a clock error or unavailable state. A process
restart preserves the original deadline. A host restart requires an advancing
pulse before the status returns to OK.

Configure recurring execution and route nonzero results and recovery to the
approved incident destination. The polling interval adds to the 600-second
threshold before delivery. Monitor missed executions from a separate service
so an outage of the receiver host itself can also be detected.

On systemd, start the timer after observing the first native pulses.

```sh
systemctl start oriso-signoz-heartbeat-check.service
systemctl enable --now oriso-signoz-heartbeat-check.timer
systemctl show oriso-signoz-heartbeat-check.service \
  -p Result -p ExecMainStatus -p ExecMainExitTimestamp
journalctl -u oriso-signoz-heartbeat-check.service -n 10 --no-pager
```

The timer checks every 60 seconds, including after a failed check or a stopped
receiver. A critical check exits 2 and logs its reason. A fresh pulse lets
the next check succeed. Use the result and its timestamp for incident and
recovery delivery, and verify the timer's execution from a separate host.

Before enabling incidents, observe native pulses across multiple delivery
intervals. In the approved outage rehearsal, verify one incident after the
deadline, no extension from a replay, and recovery on a new native pulse.
Put the monitor in maintenance before deliberately disabling the sender or
migrating its receiver state.
