#!/usr/bin/env python3
"""Receive a Dev SigNoz pulse or report its state from the external runner."""

import argparse
from base64 import b64encode
from contextlib import closing
from decimal import Decimal, InvalidOperation
from functools import cache
from hmac import compare_digest
from http import HTTPStatus
import json
from math import isfinite
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from typing import NamedTuple
from uuid import UUID


DEADLINE = 600
MAX_BODY = 16 * 1024
DB_TIMEOUT = 2
CLOCK_ERRORS = (OSError, ValueError, subprocess.SubprocessError)
EXPECTED_LABELS = {
    "alertname": "ORISO dev | SigNoz heartbeat",
    "environment": "dev",
    "deployment.environment": "dev",
    "managed_by": "oriso_helm",
}


class Clock(NamedTuple):
    boot_id: str
    wall: float
    monotonic: float


@cache
def boot_id() -> str:
    if sys.platform == "darwin":
        value = subprocess.check_output(
            ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"], text=True, timeout=2,
        )
    else:
        value = Path("/proc/sys/kernel/random/boot_id").read_text()
    return str(UUID(value.strip()))


def current_clock() -> Clock:
    return Clock(boot_id(), time.time(), time.monotonic())


def initialize(path: Path) -> None:
    with closing(sqlite3.connect(path, timeout=DB_TIMEOUT)) as db, db:
        if db.execute("PRAGMA journal_mode = DELETE").fetchone() != ("delete",):
            raise sqlite3.OperationalError("heartbeat database journal mode is unavailable")
        db.execute("PRAGMA synchronous = EXTRA")
        db.execute(
            """CREATE TABLE IF NOT EXISTS heartbeat (
                   cluster TEXT PRIMARY KEY,
                   evaluated_at INTEGER NOT NULL,
                   boot_id TEXT NOT NULL,
                   received_monotonic REAL NOT NULL CHECK(received_monotonic >= 0)
               )""",
        )


def record(path: Path, cluster: str, stamp: int, clock: Clock) -> None:
    # The UPSERT compares and writes under the same SQLite write transaction.
    # The connection context commits before the caller acknowledges the pulse.
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, timeout=DB_TIMEOUT)) as db, db:
        db.execute("PRAGMA synchronous = EXTRA")
        db.execute(
            """INSERT INTO heartbeat (cluster, evaluated_at, boot_id, received_monotonic)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(cluster) DO UPDATE SET
                   evaluated_at = excluded.evaluated_at,
                   boot_id = excluded.boot_id,
                   received_monotonic = excluded.received_monotonic
               WHERE excluded.evaluated_at > heartbeat.evaluated_at
                 AND (excluded.boot_id != heartbeat.boot_id
                      OR excluded.received_monotonic >= heartbeat.received_monotonic)""",
            (cluster, stamp, clock.boot_id, clock.monotonic),
        )


def read_status(path: Path, cluster: str, clock=current_clock) -> dict:
    critical = {"state": "CRITICAL"}
    try:
        path.stat()
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=DB_TIMEOUT)) as db:
            row = db.execute(
                "SELECT boot_id, received_monotonic FROM heartbeat WHERE cluster = ?", (cluster,),
            ).fetchone()
    except FileNotFoundError:
        return {**critical, "reason": "no_pulse"}
    except (OSError, sqlite3.Error):
        return {**critical, "reason": "state_unavailable"}
    if row is None:
        return {**critical, "reason": "no_pulse"}
    saved_boot, received = row
    if not isinstance(received, (int, float)) or not isfinite(received) or received < 0:
        return {**critical, "reason": "state_unavailable"}
    now = clock()
    if saved_boot != now.boot_id:
        return {**critical, "reason": "host_restarted"}
    age = now.monotonic - received
    if not isfinite(age) or age < 0:
        return {**critical, "reason": "clock_error"}
    if age >= DEADLINE:
        return {**critical, "reason": "silence", "seconds_since_pulse": int(age)}
    return {"state": "OK", "reason": "fresh", "seconds_since_pulse": int(age)}


def unique_object(pairs: list[tuple]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON object key")
    return result


def reject_constant(_value: str) -> None:
    raise ValueError("nonfinite JSON constant")


def evaluation_stamp(payload: object, cluster: str, received_at: float) -> int | None:
    try:
        if (
            payload["version"] != "4" or payload["status"] != "firing"
            or type(payload["truncatedAlerts"]) is not int or payload["truncatedAlerts"] != 0
            or not isinstance(payload["alerts"], list)
        ):
            return None
        alert, = payload["alerts"]
        labels = alert["labels"]
        if (
            alert["status"] != "firing" or labels["k8s.cluster.name"] != cluster
            or any(labels.get(name) != value for name, value in EXPECTED_LABELS.items())
        ):
            return None
        value = alert["annotations"]["oriso_evaluated_at"]
        if not isinstance(value, str) or len(value) > 64:
            return None
        stamp = Decimal(value)
        # Bound the numeric range before converting scientific notation to an integer.
        if not stamp.is_finite() or not received_at - 180 <= stamp <= received_at + 5:
            return None
        return int(stamp) if stamp == stamp.to_integral_value() else None
    except (AttributeError, InvalidOperation, KeyError, TypeError, ValueError):
        return None


def create_app(path: Path, cluster: str, password: bytes, *, clock=current_clock):
    initialize(path)
    authorization = b"Basic " + b64encode(b"heartbeat:" + password)

    def accept(environ: dict) -> HTTPStatus:
        if environ.get("PATH_INFO") != "/pulse":
            return HTTPStatus.NOT_FOUND
        if environ.get("REQUEST_METHOD") != "POST":
            return HTTPStatus.METHOD_NOT_ALLOWED
        supplied = environ.get("HTTP_AUTHORIZATION", "").encode("utf-8")
        if not compare_digest(supplied, authorization):
            return HTTPStatus.UNAUTHORIZED
        content_type = environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json" or environ.get("HTTP_CONTENT_ENCODING", "identity") != "identity":
            return HTTPStatus.UNSUPPORTED_MEDIA_TYPE
        try:
            size = int(environ.get("CONTENT_LENGTH", ""))
            if size > MAX_BODY:
                return HTTPStatus.REQUEST_ENTITY_TOO_LARGE
            if size <= 0:
                return HTTPStatus.BAD_REQUEST
            raw = environ["wsgi.input"].read(size)
            if len(raw) != size:
                return HTTPStatus.BAD_REQUEST
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant)
        except (OSError, RecursionError, ValueError):
            return HTTPStatus.BAD_REQUEST

        try:
            now = clock()
            stamp = evaluation_stamp(payload, cluster, now.wall)
            if stamp is not None:
                record(path, cluster, stamp, now)
        except (*CLOCK_ERRORS, sqlite3.Error):
            return HTTPStatus.SERVICE_UNAVAILABLE
        return HTTPStatus.NO_CONTENT

    def app(environ, start_response):
        result = accept(environ)
        headers = []
        if result != HTTPStatus.NO_CONTENT:
            headers.append(("Content-Length", "0"))
        if result == HTTPStatus.UNAUTHORIZED:
            headers.append(("WWW-Authenticate", 'Basic realm="SigNoz heartbeat"'))
        if result == HTTPStatus.METHOD_NOT_ALLOWED:
            headers.append(("Allow", "POST"))
        start_response(f"{result.value} {result.phrase}", headers)
        return [b""]

    return app


def settings() -> tuple[Path, str]:
    path = Path(os.environ["SIGNOZ_HEARTBEAT_DB"])
    cluster = os.environ["SIGNOZ_HEARTBEAT_CLUSTER"]
    if not path.is_absolute() or not cluster or cluster.strip() != cluster:
        raise ValueError("heartbeat requires an absolute database path and a cluster name")
    return path, cluster


def from_environment():
    """WSGI factory. Only the receiver process reads the webhook secret."""
    path, cluster = settings()
    with Path(os.environ["SIGNOZ_HEARTBEAT_PASSWORD_FILE"]).open("rb") as stream:
        password = stream.read(1026).rstrip(b"\r\n")
    if not 0 < len(password) <= 1024 or b"\n" in password or b"\r" in password:
        raise ValueError("heartbeat password file must contain one nonempty line")
    return create_app(path, cluster, password)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the persisted external Dev SigNoz heartbeat.")
    parser.parse_args(argv)
    try:
        path, cluster = settings()
        status = read_status(path, cluster, current_clock)
    except (KeyError, *CLOCK_ERRORS):
        status = {"state": "CRITICAL", "reason": "probe_configuration"}
    print(json.dumps(status, sort_keys=True))
    return 0 if status["state"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
