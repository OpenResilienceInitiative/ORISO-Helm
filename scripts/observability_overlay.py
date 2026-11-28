#!/usr/bin/env python3
"""Print the observability values and ClickHouse ingress policy of an environment.

Live Dev is deployed from a hand-kept base values.yaml that predates several
chart defaults, and values-dev.yaml also carries non-observability deltas that
Dev does not run today (Matrix server name, dummy data). Applying the whole
overlay would change those too. This prints just the observability keys and
ClickHouse ingress policy, with chart defaults first and the environment overlay
on top, so SigNoz can be
rolled out on its own:

    python3 scripts/observability_overlay.py values-dev.yaml > /tmp/observability-dev.yaml
    helm upgrade caritas . -n caritas -f values.yaml -f /tmp/observability-dev.yaml -f secrets.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

CHART_DIR = Path(__file__).resolve().parent.parent
# (path, key) pairs taken over; everything else stays with the base values.
KEYS = (
    ("global", "observability"), ("signoz",), ("signozCollector",),
    ("dbMonitoring",), ("networkPolicies", "clickhouseIngress"),
)
ABSENT = object()


def merge(base: Any, override: Any) -> Any:
    if override is ABSENT:
        return base
    if base is ABSENT:
        return override
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = merge(base.get(key, ABSENT), value)
        return merged
    return override


def pick(values: dict[str, Any], path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(values, dict) or key not in values:
            return ABSENT
        values = values[key]
    return values


def overlay(*environment_files: Path, include_defaults: bool = True) -> dict[str, Any]:
    defaults = {}
    if include_defaults:
        defaults = yaml.safe_load((CHART_DIR / "values.yaml.default").read_text(encoding="utf-8")) or {}
    environment = {}
    for path in environment_files:
        environment = merge(environment, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    out: dict[str, Any] = {}
    for path in KEYS:
        value = merge(pick(defaults, path), pick(environment, path))
        if value is ABSENT:
            continue
        target = out
        for key in path[:-1]:
            target = target.setdefault(key, {})
        target[path[-1]] = value
    return out


def differing_paths(expected: Any, actual: Any, path: tuple[str, ...] = ()) -> list[str]:
    if isinstance(expected, dict) and expected and isinstance(actual, dict):
        return [
            mismatch
            for key, value in expected.items()
            for mismatch in differing_paths(value, actual.get(key, ABSENT), (*path, key))
        ]
    if type(expected) is type(actual) and expected == actual:
        return []
    return [".".join(path)]



def unexpected_signal_backup_paths(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    signal = ("signoz", "orisoDataProtection", "signalBackup")
    clickhouse = ("signoz", "clickhouse")
    if pick(expected, (*signal, "enabled")) is not False or pick(actual, (*clickhouse, "enabled")) is False:
        return []

    disk_path = (*clickhouse, "files", "config.d/97-oriso-signal-backup-disk.xml")
    volumes_path = (*clickhouse, "additionalVolumes")
    mounts_path = (*clickhouse, "additionalVolumeMounts")
    claim = pick(actual, (*signal, "claim"))
    if claim is ABSENT:
        claim = "signoz-clickhouse-signal-backup"

    disk = pick(actual, disk_path)
    volumes = pick(actual, volumes_path)
    mounts = pick(actual, mounts_path)
    has_signal_volume = isinstance(volumes, list) and any(
        isinstance(volume, dict)
        and pick(volume, ("persistentVolumeClaim", "claimName")) == claim
        for volume in volumes
    )
    has_signal_mount = isinstance(mounts, list) and any(
        isinstance(mount, dict)
        and str(mount.get("mountPath", "")).rstrip("/") == "/var/lib/clickhouse-signal-backup"
        for mount in mounts
    )
    return [
        ".".join(path)
        for path, present in (
            (disk_path, isinstance(disk, str) and "oriso_signal_backup" in disk),
            (volumes_path, has_signal_volume),
            (mounts_path, has_signal_mount),
        )
        if present and pick(expected, path) is ABSENT
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environment_files", type=Path, nargs="+")
    parser.add_argument(
        "--check-effective",
        action="store_true",
        help="verify declared observability and ClickHouse ingress policy values against Helm values on stdin",
    )
    args = parser.parse_args()
    files = [p if p.is_absolute() else CHART_DIR / p for p in args.environment_files]
    source_names = " + ".join(p.name for p in files)
    if args.check_effective:
        try:
            actual = yaml.safe_load(sys.stdin)
        except yaml.YAMLError:
            actual = None
        if not isinstance(actual, dict):
            print("Cannot read effective Helm values as a mapping", file=sys.stderr)
            return 1
        expected = overlay(*files, include_defaults=False)
        if not expected:
            print("No rollout values declared in the environment overlay", file=sys.stderr)
            return 1
        mismatches = differing_paths(expected, actual)
        mismatches.extend(unexpected_signal_backup_paths(expected, actual))
        if mismatches:
            print("Missing or different rollout values:", file=sys.stderr)
            print(*sorted(mismatches), sep="\n", file=sys.stderr)
            return 1
        print(f"PASS: effective rollout values match {source_names}")
        return 0
    print(f"# Generated from values.yaml.default + {source_names}; do not edit.")
    print(yaml.safe_dump(overlay(*files), sort_keys=False), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
