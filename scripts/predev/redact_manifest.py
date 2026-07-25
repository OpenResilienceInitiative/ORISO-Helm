#!/usr/bin/env python3
"""Create an artifact-safe manifest with all Secret values removed."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    documents = []
    for document in yaml.safe_load_all(args.source.read_text()):
        if not isinstance(document, dict):
            continue
        if document.get("kind") == "Secret":
            for field in ("data", "stringData"):
                values = document.get(field)
                if isinstance(values, dict):
                    document[field] = {key: "<redacted>" for key in values}
        documents.append(document)

    args.output.write_text(yaml.safe_dump_all(documents, sort_keys=True))


if __name__ == "__main__":
    main()
