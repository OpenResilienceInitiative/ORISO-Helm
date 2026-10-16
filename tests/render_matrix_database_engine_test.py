#!/usr/bin/env python3
"""Render guard: Synapse's database engine is an explicit opt-in.

A helm upgrade must never move an install with Matrix data from SQLite to
PostgreSQL by itself (ORISO-Helm#282, review on #426). The default stays
sqlite3; postgres is rendered only when matrix.database.engine says so, and an
unknown engine fails the render.
"""

from __future__ import annotations

import os
import subprocess

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VALUE_FILES = (
    "values.yaml.default",
    "tests/fixtures/values-render-domain.yaml",
    "secrets.yaml.default",
    "tests/fixtures/render-required-secrets.yaml",
)
PG_PASSWORD = "render-test-pg-password-0001"


def render(*extra: str) -> subprocess.CompletedProcess:
    cmd = ["helm", "template", "matrix-db", CHART_DIR]
    for name in VALUE_FILES:
        cmd += ["-f", os.path.join(CHART_DIR, name)]
    cmd += ["--set-string", f"postgres.postgresPassword={PG_PASSWORD}", *extra]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def database_block(stdout: str) -> dict:
    docs = [d for d in yaml.safe_load_all(stdout) if isinstance(d, dict)]
    secret = next(
        d for d in docs
        if d["kind"] == "Secret" and d["metadata"]["name"] == "matrix-postgres-secret"
    )
    for d in docs:
        if d["kind"] == "ConfigMap":
            assert PG_PASSWORD not in yaml.safe_dump(d), f"password in ConfigMap {d['metadata']['name']}"
    return yaml.safe_load(secret["stringData"]["homeserver-database.yaml"])["database"]


def main() -> None:
    default = render()
    assert default.returncode == 0, default.stderr
    db = database_block(default.stdout)
    assert db == {"name": "sqlite3", "args": {"database": "/data/homeserver.db"}}, db

    # An environment values.yaml copied before the key existed must stay on SQLite.
    legacy = render("--set", "matrix.database=null")
    assert legacy.returncode == 0, legacy.stderr
    assert database_block(legacy.stdout)["name"] == "sqlite3"

    pg = render("--set-string", "matrix.database.engine=postgres")
    assert pg.returncode == 0, pg.stderr
    db = database_block(pg.stdout)
    assert db["name"] == "psycopg2", db["name"]
    assert db["args"]["host"] == "matrix-postgres-service", db["args"]["host"]
    assert db["args"]["password"] == PG_PASSWORD

    bad = render("--set-string", "matrix.database.engine=mysql")
    assert bad.returncode != 0 and "matrix.database.engine" in bad.stderr, bad.stderr
    print("matrix database engine: OK")


if __name__ == "__main__":
    main()
