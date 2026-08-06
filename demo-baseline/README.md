# ORISO Demo Baseline

This directory contains the Helm-owned demo/initial-delivery baseline for
customer-facing ORISO demo environments.

It is not arbitrary production customer data. Its job is to make demo readiness
reproducible and checkable before a customer demo.

## One-command demo gate

Run the full pre-demo gate against a deployed environment:

```bash
DEMO_BASE_URL=https://api.oriso.org ./scripts/demo-baseline-gate.sh all
```

Defaults:

- `NAMESPACE=caritas`
- `MARIADB_SECRET=mariadb-secret`
- `MARIADB_POD_SELECTOR=app=mariadb`
- `MARIADB_USER=root`

The script reads `MYSQL_ROOT_PASSWORD` from the Kubernetes secret unless
`MARIADB_PASSWORD` is already set.

## Actions

```bash
./scripts/demo-baseline-gate.sh check
./scripts/demo-baseline-gate.sh sync
DEMO_BASE_URL=https://api.oriso.org ./scripts/demo-baseline-gate.sh smoke
DEMO_BASE_URL=https://api.oriso.org ./scripts/demo-baseline-gate.sh all
```

- `check` fails when the live database has drifted away from the expected demo
  baseline.
- `sync` applies the idempotent SQL baseline.
- `smoke` checks the public registration API for the required postcode/topic
  visibility paths.
- `all` runs `sync`, `check`, and `smoke`.

## Required visible paths

The drift gate currently protects these public registration paths:

- postcode `88885`, consulting type `1`, topic `2`:
  `Kinder und Jugendliche`
- postcode `88885`, consulting type `1`, topic `10`:
  `Eltern und Familie`

Both paths must have at least one online, not-deleted agency. The current demo
baseline uses agency `246` (`Caritasverband Wismar`) and links it to both
topics.

## Change method

When the expected demo baseline changes, update these files in the same commit:

1. `demo-baseline/manifest.json`
2. `demo-baseline/demo-baseline-sync.sql`
3. `demo-baseline/demo-baseline-check.sql`
4. `tests/test_demo_baseline.py`

Run:

```bash
python3 -m unittest tests/test_demo_baseline.py
```

The test intentionally fails when the manifest names a required topic/path that
the SQL or shell gate does not cover. That is the local alert that the script
must be changed together with the baseline.
