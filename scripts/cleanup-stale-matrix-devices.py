#!/usr/bin/env python3
"""One-off removal of stale Matrix devices (ORISO-Helm#628).

Synapse's delete_stale_devices_after only removes devices whose last_seen is
older than the limit, and only from the time it is configured. This script
clears the backlog once:

* devices whose last_seen is older than --older-than-days (default 30),
* with --include-never-seen, devices that were never seen (last_seen null),
* with --include-old-admin-devices USER, every device of that technical user
  except the one the operator token is using (the UserService admin login
  created one device per token refresh before ORISO-UserService#1756).

Dry-run is the default. It prints how many devices each user would lose.
Deleting needs --apply and the exact confirmation phrase.

Required environment (never pass the token as an argument):
  SYNAPSE_ADMIN_URL    Synapse base URL reachable by the operator,
                       e.g. http://matrix-synapse:8008
  SYNAPSE_ADMIN_TOKEN  Server-admin access token.

Usage:
  scripts/cleanup-stale-matrix-devices.py
  scripts/cleanup-stale-matrix-devices.py --include-never-seen \
      --include-old-admin-devices @matrixadm:matrix.oriso.org
  scripts/cleanup-stale-matrix-devices.py ... --apply \
      --confirm DELETE_STALE_MATRIX_DEVICES

Deleting a device signs that browser out of Matrix and drops the encryption
keys stored for it. Run it only with the environment owner's go.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

CONFIRMATION = "DELETE_STALE_MATRIX_DEVICES"
PAGE_SIZE = 100
DELETE_BATCH = 100
DAY_MS = 24 * 60 * 60 * 1000


def select_stale_devices(
    devices: list[dict],
    *,
    now_ms: int,
    older_than_days: int,
    include_never_seen: bool,
    delete_all_but: str | None,
) -> list[str]:
    """Device ids to delete for one user. delete_all_but keeps exactly that device."""
    if delete_all_but is not None:
        return [d["device_id"] for d in devices if d.get("device_id") != delete_all_but]
    cutoff = now_ms - older_than_days * DAY_MS
    selected = []
    for device in devices:
        last_seen = device.get("last_seen_ts")
        if last_seen is None:
            if include_never_seen:
                selected.append(device["device_id"])
        elif last_seen < cutoff:
            selected.append(device["device_id"])
    return selected


class Synapse:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}

    def whoami_device(self) -> str | None:
        return self.request("GET", "/_matrix/client/v3/account/whoami").get("device_id")

    def users(self):
        start = "0"
        while start is not None:
            page = self.request(
                "GET",
                f"/_synapse/admin/v2/users?from={start}&limit={PAGE_SIZE}&deactivated=false",
            )
            yield from (user["name"] for user in page.get("users", []))
            start = page.get("next_token")

    def devices(self, user_id: str) -> list[dict]:
        quoted = urllib.parse.quote(user_id, safe="")
        return self.request("GET", f"/_synapse/admin/v2/users/{quoted}/devices").get("devices", [])

    def delete_devices(self, user_id: str, device_ids: list[str]) -> None:
        quoted = urllib.parse.quote(user_id, safe="")
        for i in range(0, len(device_ids), DELETE_BATCH):
            self.request(
                "POST",
                f"/_synapse/admin/v2/users/{quoted}/delete_devices",
                {"devices": device_ids[i : i + DELETE_BATCH]},
            )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--older-than-days", type=int, default=30)
    parser.add_argument("--include-never-seen", action="store_true")
    parser.add_argument("--include-old-admin-devices", metavar="USER", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)

    if args.older_than_days < 7:
        parser.error("--older-than-days must be at least 7")
    if args.apply and args.confirm != CONFIRMATION:
        parser.error(f"--apply needs --confirm {CONFIRMATION}")
    base_url = os.environ.get("SYNAPSE_ADMIN_URL")
    token = os.environ.get("SYNAPSE_ADMIN_TOKEN")
    if not base_url or not token:
        parser.error("SYNAPSE_ADMIN_URL and SYNAPSE_ADMIN_TOKEN must be set")

    synapse = Synapse(base_url, token)
    own_device = synapse.whoami_device()
    now_ms = int(time.time() * 1000)
    admin_users = set(args.include_old_admin_devices)
    total_devices = total_selected = 0

    for user_id in synapse.users():
        devices = synapse.devices(user_id)
        selected = select_stale_devices(
            devices,
            now_ms=now_ms,
            older_than_days=args.older_than_days,
            include_never_seen=args.include_never_seen,
            delete_all_but=own_device if user_id in admin_users else None,
        )
        # The operator token's own device is never deleted.
        selected = [d for d in selected if d != own_device]
        total_devices += len(devices)
        total_selected += len(selected)
        if selected:
            print(f"{user_id}: {len(selected)} of {len(devices)} devices")
            if args.apply:
                synapse.delete_devices(user_id, selected)

    action = "deleted" if args.apply else "would delete (dry-run)"
    print(f"total: {action} {total_selected} of {total_devices} devices")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
