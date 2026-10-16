#!/usr/bin/env python3
"""clear-helm.sh keeps observability volumes and stays inside its namespace."""

from __future__ import annotations

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
        "signoz-db-caritas-signoz-0|",
        "data-volumeclaim-template-chi-caritas-clickhouse-cluster-0-0-0|",
        "data-caritas-zookeeper-0|",
        "caritas-signoz-data-protection-backup|keep",
        "operator-kept-claim|keep",
    ]
)

PVS = "\n".join(
    [
        "pv-mariadb|Released|caritas|data-caritas-mariadb-0",
        "pv-signoz|Released|caritas|signoz-db-caritas-signoz-0",
        "pv-clickhouse|Bound|caritas|data-volumeclaim-template-chi-caritas-clickhouse-cluster-0-0-0",
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
        self._stub(
            "helm",
            """
            echo "helm $*" >> "$STUB_DIR/calls.log"
            [ "$1" = status ] && exit 0
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
                "kubectl delete pv pv-mariadb",
            ],
        )
        self.assertIn("Keeping PVC signoz-db-caritas-signoz-0", result.stdout)
        self.assertIn("Keeping PVC operator-kept-claim", result.stdout)
        self.assertIn("Keeping PV pv-signoz", result.stdout)

    def test_dry_run_deletes_nothing(self) -> None:
        result, deletes = self._run("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(deletes, [])
        self.assertIn(
            "[dry-run] kubectl delete pvc data-caritas-mariadb-0 -n caritas",
            result.stdout,
        )
        self.assertIn("Dry run: nothing was deleted.", result.stdout)

    def test_include_observability_deletes_everything_of_the_namespace(self) -> None:
        result, deletes = self._run("--include-observability")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            deletes,
            [
                "helm uninstall caritas -n caritas",
                "kubectl delete pvc data-caritas-mariadb-0 -n caritas",
                "kubectl delete pvc signoz-db-caritas-signoz-0 -n caritas",
                "kubectl delete pvc data-volumeclaim-template-chi-caritas-clickhouse-cluster-0-0-0 -n caritas",
                "kubectl delete pvc data-caritas-zookeeper-0 -n caritas",
                "kubectl delete pvc caritas-signoz-data-protection-backup -n caritas",
                "kubectl delete pvc operator-kept-claim -n caritas",
                "kubectl delete pv pv-mariadb",
                "kubectl delete pv pv-signoz",
            ],
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

    def test_unknown_argument_is_refused(self) -> None:
        result, deletes = self._run("--yes")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(deletes, [])


if __name__ == "__main__":
    unittest.main()
