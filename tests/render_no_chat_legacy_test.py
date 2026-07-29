#!/usr/bin/env python3
"""Prevent Rocket.Chat and the retired embedded-Jitsi stack from returning."""

from __future__ import annotations

import os
import subprocess
import sys

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORBIDDEN = (
    "rocket_chat",
    "rocket-chat",
    "rocketchat",
    "rc_user_id",
    "rc_group_id",
    "jitsi-meet",
    "jitsi-jvb",
    "jitsi-prosody",
    "jicofo",
    "prosody",
)


def main() -> None:
    checked_files = [
        os.path.join(CHART_DIR, "README.md"),
        os.path.join(CHART_DIR, "values.yaml.default"),
        os.path.join(CHART_DIR, "secrets.yaml.default"),
        os.path.join(
            CHART_DIR,
            "charts",
            "mariadb",
            "sql-schemas",
            "userservice-schema.sql",
        ),
    ]
    for root, _, files in os.walk(os.path.join(CHART_DIR, "templates")):
        checked_files.extend(
            os.path.join(root, filename)
            for filename in files
            if filename.endswith((".yaml", ".yml", ".tpl"))
        )

    for path in checked_files:
        with open(path, encoding="utf-8") as source:
            lowered = source.read().lower()
        matches = [term for term in FORBIDDEN if term in lowered]
        assert not matches, f"{os.path.relpath(path, CHART_DIR)} contains {matches}"

    result = subprocess.run(
        [
            "helm",
            "template",
            "no-chat-legacy",
            CHART_DIR,
            "-f",
            os.path.join(CHART_DIR, "values.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "secrets.yaml.default"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    rendered = result.stdout.lower()
    matches = [term for term in FORBIDDEN if term in rendered]
    assert not matches, f"rendered chart contains {matches}"

    print(
        "PASS: Helm source and rendered resources contain no Rocket.Chat "
        "or retired embedded-Jitsi contracts"
    )


if __name__ == "__main__":
    try:
        main()
    except AssertionError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
