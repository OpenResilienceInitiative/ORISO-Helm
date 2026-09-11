#!/usr/bin/env python3
"""Guard the presence of the per-environment values overlays.

The render contracts in this directory layer `values-<env>.yaml` on top of the
base values. When an overlay disappears, `helm template` fails on a missing
file and every render contract aborts on the first test — the validation
workflow then reports a generic red without naming the real cause.

`values-prod.yaml` has no render contract of its own yet, so nothing else in
this suite notices when it goes missing.
"""

from __future__ import annotations

import os
import sys

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REQUIRED_OVERLAYS = (
    "values-dev.yaml",
    "values-pre-dev.yaml",
    "values-prod.yaml",
)


def assert_environment_overlays_exist() -> None:
    missing = [
        name
        for name in REQUIRED_OVERLAYS
        if not os.path.isfile(os.path.join(CHART_DIR, name))
    ]
    assert not missing, (
        "missing environment values overlay(s): "
        + ", ".join(missing)
        + ". The render contracts layer these on top of values.yaml.default; "
        "deleting one makes helm template fail on a missing file."
    )


def main() -> None:
    assert_environment_overlays_exist()


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
