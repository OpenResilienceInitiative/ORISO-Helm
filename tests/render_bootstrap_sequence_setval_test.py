#!/usr/bin/env python3
"""Regression guard for bootstrap sequence sync SQL."""

from __future__ import annotations

import os
import re
import sys

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_sql(path: str) -> str:
    with open(os.path.join(CHART_DIR, path), encoding="utf-8") as sql_file:
        return sql_file.read()


def main() -> None:
    tenant_sql = read_sql("files/tenant-bootstrap.sql")
    topic_sql = read_sql("files/topic-bootstrap.sql")

    assert "DO SETVAL(`sequence_tenant`, 1, 1);" in tenant_sql
    assert "DO SETVAL(`sequence_topic`, 20, 1);" in topic_sql
    assert "DO SETVAL(`sequence_topic_group`, 6, 1);" in topic_sql

    for path, sql in {
        "files/tenant-bootstrap.sql": tenant_sql,
        "files/topic-bootstrap.sql": topic_sql,
    }.items():
        for call in re.findall(r"DO SETVAL\((.*?)\);", sql):
            assert "@" not in call, f"{path} SETVAL must not use a session variable: {call}"

    print("PASS: bootstrap SETVAL calls use fixed seeded ids")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
