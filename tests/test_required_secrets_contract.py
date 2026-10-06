#!/usr/bin/env python3
"""The shipped defaults must not declare a mapping key twice.

`secrets.yaml.default` once declared `tenantService:` twice. YAML keeps the last
mapping, so the second block silently discarded `smtpPasswordEncryptionSecret`
from the first, together with the comment documenting it as required. Reading
the file the key was plainly there; resolving the file it was gone, and nothing
downstream could report it because the parser accepts the duplicate.

The check parses with a loader that refuses duplicates, so it follows real YAML
semantics at every nesting level instead of matching lines.
"""

from __future__ import annotations

import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULTS = ("values.yaml.default", "secrets.yaml.default")


class StrictLoader(yaml.SafeLoader):
    """SafeLoader that raises on a repeated key instead of keeping the last."""


def _construct_mapping(loader: StrictLoader, node: yaml.MappingNode, deep: bool = False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"duplicate key {key!r}: every earlier value is silently discarded",
                key_node.start_mark,
            )
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


class DefaultsDeclareEachKeyOnce(unittest.TestCase):
    def test_no_mapping_key_is_declared_twice(self):
        for name in DEFAULTS:
            with self.subTest(file=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                try:
                    yaml.load(text, Loader=StrictLoader)
                except yaml.constructor.ConstructorError as error:
                    self.fail(f"{name}: {error.problem} (line {error.problem_mark.line + 1})")

    def test_the_loader_does_catch_the_original_defect(self):
        """Guards the guard: a loader that never raises would pass the test above."""
        duplicated = "tenantService:\n  a: 1\ntenantService:\n  b: 2\n"
        with self.assertRaises(yaml.constructor.ConstructorError):
            yaml.load(duplicated, Loader=StrictLoader)

        nested = "outer:\n  inner: 1\n  inner: 2\n"
        with self.assertRaises(yaml.constructor.ConstructorError):
            yaml.load(nested, Loader=StrictLoader)


if __name__ == "__main__":
    unittest.main()
