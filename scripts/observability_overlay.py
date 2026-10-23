#!/usr/bin/env python3
"""Print only the observability values of an environment, as a values file.

Live Dev is deployed from a hand-kept base values.yaml that predates several
chart defaults, and values-dev.yaml also carries non-observability deltas that
Dev does not run today (Matrix server name, dummy data). Applying the whole
overlay would change those too. This prints just the observability keys,
chart defaults first and the environment overlay on top, so SigNoz can be
rolled out on its own:

    python3 scripts/observability_overlay.py values-dev.yaml > /tmp/observability-dev.yaml
    helm upgrade caritas . -n caritas -f values.yaml -f /tmp/observability-dev.yaml -f secrets.yaml
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

CHART_DIR = Path(__file__).resolve().parent.parent
# (path, key) pairs taken over; everything else stays with the base values.
KEYS = (("global", "observability"), ("signoz",), ("signozCollector",), ("dbMonitoring",))
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


def overlay(environment_file: Path) -> dict[str, Any]:
    defaults = yaml.safe_load((CHART_DIR / "values.yaml.default").read_text(encoding="utf-8")) or {}
    environment = yaml.safe_load(environment_file.read_text(encoding="utf-8")) or {}
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


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: observability_overlay.py <environment values file>")
    environment_file = Path(sys.argv[1])
    if not environment_file.is_absolute():
        environment_file = CHART_DIR / environment_file
    print(f"# Generated from values.yaml.default + {environment_file.name}; do not edit.")
    print(yaml.safe_dump(overlay(environment_file), sort_keys=False), end="")


if __name__ == "__main__":
    main()
