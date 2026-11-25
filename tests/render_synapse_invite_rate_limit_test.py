#!/usr/bin/env python3
"""Render guard for Synapse rc_invites (ORISO-UserService#1736).

Every new enquiry gets an agency holding room, and UserService invites every
counsellor of the agency into it. With Synapse's shipped invite limits a single
enquiry of a ~17-counsellor agency exhausts the room budget, and counsellors and
supervisors who receive many invites hit the per-user limit (~5.5 minutes per
invite). The chart must render rc_invites from values, fall back to Synapse's
shipped defaults when nothing is set, and Dev must carry its raised values.
"""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELM_TIMEOUT_SECONDS = 180

# Synapse's shipped rc_invites defaults (homeserver config documentation).
SYNAPSE_DEFAULTS = {
    "per_room": {"per_second": 0.3, "burst_count": 10},
    "per_user": {"per_second": 0.003, "burst_count": 5},
    "per_issuer": {"per_second": 0.3, "burst_count": 10},
}
DEV_INVITE_LIMITS = {
    "per_room": {"per_second": 1.0, "burst_count": 40},
    "per_user": {"per_second": 0.1, "burst_count": 20},
    "per_issuer": {"per_second": 1.0, "burst_count": 40},
}
HOLDING_ROOM_COUNSELLORS = 17
# Synapse's shipped rc_message: also limits room creation and membership events per user
# (ORISO-UserService#1737: the agency account's backfill invites used it up, so the next
# enquiry's room creation was refused with a ~3 s retry).
SYNAPSE_MESSAGE_DEFAULT = {"per_second": 0.2, "burst_count": 10}


def render_documents(*extra: str) -> list[dict]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "synapse-invite-limit",
            CHART_DIR,
            "-f",
            os.path.join(CHART_DIR, "values.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "tests", "fixtures", "values-render-domain.yaml"),
            "-f",
            os.path.join(CHART_DIR, "secrets.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "tests", "fixtures", "render-required-secrets.yaml"),
            "--set-string",
            "global.secrets.redisdefaultPass=test-redis-password",
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=HELM_TIMEOUT_SECONDS,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def render_homeserver(*extra: str) -> dict:
    for document in render_documents(*extra):
        for value in (document.get("data") or {}).values():
            if isinstance(value, str) and "server_name:" in value and "rc_login" in value:
                return yaml.safe_load(value)
    raise AssertionError("no Synapse homeserver config rendered")


def main() -> int:
    failures: list[str] = []
    cases = {
        "default values keep Synapse's shipped invite limits": ([], SYNAPSE_DEFAULTS),
        # A deployment values file without the keys must not lose the limit.
        "values without rateLimits": (["--set", "matrix.rateLimits=null"], SYNAPSE_DEFAULTS),
        "override": (
            [
                "--set",
                "matrix.rateLimits.invitesPerRoom.burstCount=40",
                "--set",
                "matrix.rateLimits.invitesPerUser.perSecond=0.05",
            ],
            {
                **SYNAPSE_DEFAULTS,
                "per_room": {"per_second": 0.3, "burst_count": 40},
                "per_user": {"per_second": 0.05, "burst_count": 5},
            },
        ),
    }
    for name, (extra, expected) in cases.items():
        actual = render_homeserver(*extra).get("rc_invites")
        if actual != expected:
            failures.append(f"{name}: expected rc_invites {expected}, got {actual}")

    message_cases = {
        "default rc_message": ([], SYNAPSE_MESSAGE_DEFAULT),
        "rc_message without rateLimits": (["--set", "matrix.rateLimits=null"], SYNAPSE_MESSAGE_DEFAULT),
        "rc_message override": (
            ["--set", "matrix.rateLimits.messages.burstCount=60"],
            {"per_second": 0.2, "burst_count": 60},
        ),
    }
    for name, (extra, expected) in message_cases.items():
        actual = render_homeserver(*extra).get("rc_message")
        if actual != expected:
            failures.append(f"{name}: expected rc_message {expected}, got {actual}")

    joins_cases = {
        "default rc_joins_per_room": ([], {"per_second": 1, "burst_count": 10}),
        "rc_joins_per_room without rateLimits": (["--set", "matrix.rateLimits=null"], {"per_second": 1, "burst_count": 10}),
        "rc_joins_per_room override": (
            ["--set", "matrix.rateLimits.joinsPerRoom.burstCount=100"],
            {"per_second": 1, "burst_count": 100},
        ),
    }
    for name, (extra, expected) in joins_cases.items():
        actual = render_homeserver(*extra).get("rc_joins_per_room")
        if actual != expected:
            failures.append(f"{name}: expected rc_joins_per_room {expected}, got {actual}")

    # rc_login must be untouched by the new block.
    login = render_homeserver().get("rc_login")
    if login != {
        "address": {"per_second": 1, "burst_count": 50},
        "account": {"per_second": 0.1, "burst_count": 20},
    }:
        failures.append(f"rc_login changed: {login}")

    # Check the effective Synapse config from the production chart template and
    # the Dev overlay, including the recipient burst that can delay supervisors.
    dev = render_homeserver("-f", os.path.join(CHART_DIR, "values-dev.yaml"))
    actual_invites = dev.get("rc_invites")
    if actual_invites != DEV_INVITE_LIMITS:
        failures.append(f"Dev: expected rc_invites {DEV_INVITE_LIMITS}, got {actual_invites}")

    messages = dev.get("rc_message") or {}
    if messages.get("burst_count", 0) < 3 * HOLDING_ROOM_COUNSELLORS:
        failures.append(
            f"Dev rc_message.burst_count {messages.get('burst_count', 0)} < {3 * HOLDING_ROOM_COUNSELLORS}"
            " (agency account: room creation + backfill invites of consecutive enquiries)"
        )
    if messages.get("per_second", 0) < 1:
        failures.append(f"Dev rc_message.per_second {messages.get('per_second', 0)} < 1")

    joins = dev.get("rc_joins_per_room") or {}
    if joins.get("burst_count", 0) < 3 * HOLDING_ROOM_COUNSELLORS:
        failures.append(f"Dev rc_joins_per_room.burst_count {joins.get('burst_count', 0)} < {3 * HOLDING_ROOM_COUNSELLORS}")
    if joins.get("per_second", 0) < 2:
        failures.append(f"Dev rc_joins_per_room.per_second {joins.get('per_second', 0)} < 2")

    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("Synapse invite rate limit ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
