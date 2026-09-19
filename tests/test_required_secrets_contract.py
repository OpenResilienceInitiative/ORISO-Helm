#!/usr/bin/env python3
"""Contract tests for the shipped defaults behind the chart's `required` gates.

These exist because of a failure that was invisible in review. `secrets.yaml.default`
declared `tenantService:` twice. YAML keeps the last mapping, so the second block
silently discarded `smtpPasswordEncryptionSecret` from the first -- together with
the two comment lines documenting it as required and explaining how to generate it.

Reading the file, the key was plainly there. Resolving the file, it was gone. That
is the worst shape a configuration defect can take: the documentation of the value
and the value itself sat four lines apart, and only one of them survived parsing.

Neither test needs `helm`, so they run everywhere the unit suite runs rather than
only where the render contracts do.
"""

from __future__ import annotations

import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULTS = ("values.yaml.default", "secrets.yaml.default")

# `required "<message>" .Values.some.path`
REQUIRED = re.compile(r'required\s+"(?:[^"\\]|\\.)*"\s+\.Values\.([A-Za-z0-9_.]+)')
# `{{- if .Values.some.flag }}` -- the feature gate wrapping a whole template.
GUARD = re.compile(r'\{\{-?\s*if\s+\.Values\.([A-Za-z0-9_.]+)\s*\}\}')


def merged_defaults() -> dict:
    """Merge the shipped defaults the way `helm template -f ... -f ...` does."""
    merged: dict = {}

    def deep(into: dict, other: dict) -> None:
        for key, value in (other or {}).items():
            if isinstance(value, dict) and isinstance(into.get(key), dict):
                deep(into[key], value)
            else:
                into[key] = value

    for name in DEFAULTS:
        deep(merged, yaml.safe_load((ROOT / name).read_text()) or {})
    return merged


def lookup(values: dict, dotted: str):
    cur = values
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


class DefaultsParseToWhatTheySay(unittest.TestCase):
    def test_no_top_level_key_is_declared_twice(self):
        """A repeated top-level key drops everything the earlier block held.

        PyYAML accepts the duplicate without a warning and Helm never sees the
        lost keys, so nothing downstream can report this -- it has to be caught
        in the file itself.
        """
        for name in DEFAULTS:
            with self.subTest(file=name):
                seen, duplicated = set(), []
                for line in (ROOT / name).read_text().splitlines():
                    match = re.match(r"^([A-Za-z0-9_.-]+):", line)
                    if not match:
                        continue
                    key = match.group(1)
                    if key in seen:
                        duplicated.append(key)
                    seen.add(key)
                self.assertEqual(
                    duplicated,
                    [],
                    f"{name} declares these top-level keys more than once, and "
                    f"every key in the earlier block is silently discarded: "
                    f"{duplicated}",
                )


class RequiredGatesHaveShippedPlaceholders(unittest.TestCase):
    def test_every_active_required_gate_has_a_shipped_placeholder(self):
        """`required` turns a missing value into a failed render, not a bad deploy.

        That only helps if the shipped defaults carry a placeholder for it. A gate
        with no default fails `helm template` for everyone -- including every
        render contract in this directory -- which is how the duplicate-key bug
        surfaced.

        A gate inside a template whose feature flag ships disabled is dormant and
        exempt: enabling the feature is what makes its secret mandatory. The rule
        is derived from the flag rather than an allowlist, so turning a feature on
        turns its gate back into a requirement here too.
        """
        values = merged_defaults()
        missing = []

        for path in sorted((ROOT / "templates").rglob("*.yaml")):
            text = path.read_text()
            gates = REQUIRED.findall(text)
            if not gates:
                continue
            dormant = any(
                lookup(values, flag) in (False, None)
                for flag in GUARD.findall(text)
            )
            if dormant:
                continue
            for dotted in gates:
                if lookup(values, dotted) in (None, ""):
                    missing.append(f".Values.{dotted} ({path.relative_to(ROOT)})")

        self.assertEqual(
            missing,
            [],
            "these values are required by a template that renders under the "
            "shipped defaults, but no default supplies them, so `helm template` "
            "fails before any environment overlay is applied: " + ", ".join(missing),
        )


if __name__ == "__main__":
    unittest.main()
