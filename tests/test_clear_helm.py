#!/usr/bin/env python3
"""clear-helm.sh keeps observability volumes and stays inside its namespace."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "clear-helm.sh"

PVCS = "\n".join(
    [
        "data-caritas-mariadb-0|",
        "data-caritas-mongodb-0|",
        "signoz-db-caritas-signoz-0|",
        "data-volumeclaim-template-chi-caritas-clickhouse-cluster-0-0-0|",
        "data-volumeclaim-template-chi-caritas-clickhouse-cluster-0-1-0|",
        "data-caritas-zookeeper-0|",
        "data-caritas-zookeeper-1|",
        "caritas-signoz-data-protection-backup|keep",
        "operator-kept-claim|keep",
        # Named in the release values (existingClaim), so any name is kept.
        "own-signoz-db|",
        "own-zookeeper-log|",
        # Custom volume templates: recognised by the labels the charts set.
        "data-log-caritas-zookeeper-0||zookeeper|",
        "logs-chi-caritas-clickhouse-cluster-0-0-0|||caritas-clickhouse",
    ]
)

RELEASE_VALUES = json.dumps({"signoz": {
    "signoz": {"persistence": {"existingClaim": "own-signoz-db"}},
    "clickhouse": {"zookeeper": {"persistence": {"dataLogDir": {"existingClaim": "own-zookeeper-log"}}}},
}})


PVS = "\n".join(
    [
        "pv-mariadb|Released|caritas|data-caritas-mariadb-0",
        # Still bound: the real run frees it by deleting its claim first.
        "pv-mongodb|Bound|caritas|data-caritas-mongodb-0",
        "pv-signoz|Released|caritas|signoz-db-caritas-signoz-0",
        "pv-clickhouse|Bound|caritas|data-volumeclaim-template-chi-caritas-clickhouse-cluster-0-0-0",
        "pv-zookeeper-1|Released|caritas|data-caritas-zookeeper-1",
        "pv-operator-kept|Released|caritas|operator-kept-claim",
        "pv-own-signoz|Released|caritas|own-signoz-db",
        "pv-own-zookeeper-log|Released|caritas|own-zookeeper-log",
        "pv-zookeeper-log|Released|caritas|data-log-caritas-zookeeper-0",
        "pv-clickhouse-logs|Released|caritas|logs-chi-caritas-clickhouse-cluster-0-0-0",
        "pv-other-namespace|Released|monitoring|data-prometheus-0",
        "pv-no-claim|Available||",
        "pv-bound|Bound|caritas|data-caritas-redis-0",
    ]
)


class ClearHelmTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name)
        self.log = self.bin / "calls.log"
        (self.bin / "pvcs.txt").write_text(PVCS + "\n")
        (self.bin / "pvs.txt").write_text(PVS + "\n")
        (self.bin / "values.json").write_text(RELEASE_VALUES)
        self._stub(
            "helm",
            """
            echo "helm $*" >> "$STUB_DIR/calls.log"
            [ "$1 $2" = "get values" ] && cat "$STUB_DIR/values.json"
            exit 0
            """,
        )
        self._stub(
            "kubectl",
            """
            echo "kubectl $*" >> "$STUB_DIR/calls.log"
            case "$1 $2" in
              "get pvc") cat "$STUB_DIR/pvcs.txt" ;;
              "get pv") cat "$STUB_DIR/pvs.txt" ;;
            esac
            """,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/bash\n" + textwrap.dedent(body))
        path.chmod(0o755)

    def _run(self, *args: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "STUB_DIR": str(self.bin),
        }
        result = subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return result, [c for c in calls if " delete " in c or " uninstall " in c]

    def test_default_keeps_observability_and_stays_in_namespace(self) -> None:
        result, deletes = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            deletes,
            [
                "helm uninstall caritas -n caritas",
                "kubectl delete pvc data-caritas-mariadb-0 -n caritas",
                "kubectl delete pvc data-caritas-mongodb-0 -n caritas",
                "kubectl delete pv pv-mariadb --ignore-not-found",
                "kubectl delete pv pv-mongodb --ignore-not-found",
            ],
        )
        for claim in (
            "signoz-db-caritas-signoz-0",
            "data-volumeclaim-template-chi-caritas-clickhouse-cluster-0-1-0",
            "data-caritas-zookeeper-1",
            "operator-kept-claim",
            "own-signoz-db",
            "own-zookeeper-log",
            "data-log-caritas-zookeeper-0",
            "logs-chi-caritas-clickhouse-cluster-0-0-0",
        ):
            self.assertIn(f"Keeping PVC {claim}", result.stdout)
        for volume in ("pv-signoz", "pv-zookeeper-1", "pv-operator-kept", "pv-own-signoz",
                       "pv-own-zookeeper-log", "pv-zookeeper-log", "pv-clickhouse-logs"):
            self.assertIn(f"Keeping PV {volume}", result.stdout)

    def test_kept_claims_are_marked_for_later_runs(self) -> None:
        result, _ = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        annotated = [c for c in self.log.read_text().splitlines() if " annotate " in c]
        self.assertIn(
            "kubectl annotate pvc own-signoz-db -n caritas helm.sh/resource-policy=keep --overwrite", annotated)
        self.assertFalse([c for c in annotated if "operator-kept-claim" in c or "data-caritas-mariadb-0" in c],
                         annotated)

    def test_second_run_without_release_keeps_marked_claims(self) -> None:
        self._stub("helm", """
            echo "helm $*" >> "$STUB_DIR/calls.log"
            [ "$1" = status ] && exit 1
            exit 0
            """)
        (self.bin / "pvcs.txt").write_text("own-signoz-db|keep\ndata-caritas-mariadb-0|\n")
        (self.bin / "pvs.txt").write_text(
            "pv-mariadb|Released|caritas|data-caritas-mariadb-0\npv-own-signoz|Released|caritas|own-signoz-db\n")
        result, deletes = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(deletes, ["kubectl delete pvc data-caritas-mariadb-0 -n caritas",
                                   "kubectl delete pv pv-mariadb --ignore-not-found"])
        self.assertIn("Keeping PV pv-own-signoz", result.stdout)

    def test_dry_run_deletes_nothing_but_lists_freed_volumes(self) -> None:
        result, deletes = self._run("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(deletes, [])
        for line in (
            "[dry-run] kubectl delete pvc data-caritas-mariadb-0 -n caritas",
            "[dry-run] kubectl delete pv pv-mariadb --ignore-not-found",
            "[dry-run] kubectl delete pv pv-mongodb --ignore-not-found",
            "Dry run: nothing was deleted.",
        ):
            self.assertIn(line, result.stdout)
        self.assertNotIn("pv-bound", result.stdout)

    def test_include_observability_deletes_everything_of_the_namespace(self) -> None:
        result, deletes = self._run("--include-observability")
        self.assertEqual(result.returncode, 0, result.stderr)
        claims = [line.split("|")[0] for line in PVCS.splitlines()]
        volumes = ["pv-mariadb", "pv-mongodb", "pv-signoz", "pv-clickhouse", "pv-zookeeper-1", "pv-operator-kept",
                   "pv-own-signoz", "pv-own-zookeeper-log", "pv-zookeeper-log", "pv-clickhouse-logs"]
        self.assertEqual(
            deletes,
            ["helm uninstall caritas -n caritas"]
            + [f"kubectl delete pvc {claim} -n caritas" for claim in claims]
            + [f"kubectl delete pv {volume} --ignore-not-found" for volume in volumes],
        )

    def test_failing_kubectl_stops_before_deleting(self) -> None:
        self._stub(
            "kubectl",
            """
            echo "kubectl $*" >> "$STUB_DIR/calls.log"
            [ "$1" = get ] && exit 1
            exit 0
            """,
        )
        result, deletes = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(deletes, ["helm uninstall caritas -n caritas"])

    def test_failing_pv_query_deletes_no_volume(self) -> None:
        self._stub(
            "kubectl",
            """
            echo "kubectl $*" >> "$STUB_DIR/calls.log"
            case "$1 $2" in
              "get pvc") cat "$STUB_DIR/pvcs.txt" ;;
              "get pv") cat "$STUB_DIR/pvs.txt"; exit 1 ;;
            esac
            """,
        )
        result, _ = self._run()
        self.assertNotEqual(result.returncode, 0)
        calls = self.log.read_text().splitlines()
        queries = [i for i, call in enumerate(calls) if call.startswith("kubectl get pv ")]
        self.assertTrue(queries, calls)
        after = calls[queries[0] + 1:]
        self.assertFalse([c for c in after if " delete " in c or " uninstall " in c], calls)

    def test_unknown_argument_is_refused(self) -> None:
        result, deletes = self._run("--yes")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(deletes, [])


if __name__ == "__main__":
    unittest.main()
