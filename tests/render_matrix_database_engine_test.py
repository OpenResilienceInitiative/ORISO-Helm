#!/usr/bin/env python3
"""New values default to PostgreSQL; legacy missing keys retain SQLite."""

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


def pod_annotations(stdout: str) -> dict:
    deployment = next(
        d for d in yaml.safe_load_all(stdout)
        if isinstance(d, dict) and d["kind"] == "Deployment" and d["metadata"]["name"] == "matrix-synapse"
    )
    return deployment["spec"]["template"]["metadata"]["annotations"]


def main() -> None:
    default = render()
    assert default.returncode == 0, default.stderr
    db = database_block(default.stdout)
    assert db["name"] == "psycopg2", db

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

    fresh = default
    assert fresh.returncode == 0, fresh.stderr
    assert database_block(fresh.stdout)["name"] == "psycopg2"
    docs = [d for d in yaml.safe_load_all(fresh.stdout) if isinstance(d, dict)]
    postgres = next(d for d in docs if d["kind"] == "StatefulSet"
                    and d["metadata"]["name"] == "matrix-postgres")
    env = {e["name"]: e.get("value") for e in
           postgres["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["POSTGRES_INITDB_ARGS"] == "--encoding=UTF8 --locale=C"
    service = next(d for d in docs if d["kind"] == "Service"
                   and d["metadata"]["name"] == database_block(fresh.stdout)["args"]["host"])
    assert service["spec"]["selector"] == postgres["spec"]["template"]["metadata"]["labels"]
    assert postgres["spec"]["serviceName"] == service["metadata"]["name"]

    # Synapse reads the database block once at start, and the Secret change alone
    # does not replace the pod. Without a roll, the runbook's rollback would leave
    # Synapse running on PostgreSQL.
    key = "checksum/database-engine"
    assert pod_annotations(default.stdout)[key] == pod_annotations(pg.stdout)[key]
    assert pod_annotations(default.stdout)[key] != pod_annotations(legacy.stdout)[key]
    sqlite = render("--set-string", "matrix.database.engine=sqlite3")
    assert sqlite.returncode == 0, sqlite.stderr
    assert database_block(sqlite.stdout) == {
        "name": "sqlite3", "args": {"database": "/data/homeserver.db"}}
    assert pod_annotations(sqlite.stdout)[key] == pod_annotations(legacy.stdout)[key]

    bad = render("--set-string", "matrix.database.engine=mysql")
    assert bad.returncode != 0 and "matrix.database.engine" in bad.stderr, bad.stderr
    print("matrix database engine: OK")


if __name__ == "__main__":
    main()
